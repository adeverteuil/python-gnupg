#!/usr/bin/env python
#-*- encoding: utf-8 -*-
"""
gnupg.py
========
A Python interface to GnuPG.

This is a modified version of python-gnupg-0.3.0, which was created by Vinay
Sajip, which itself is a modification of GPG.py written by Steve Traugott,
which in turn is a modification of the pycrypto GnuPG interface written by
A.M. Kuchling.

This version is patched to exclude calls to :class:`subprocess.Popen([...],
shell=True)`, and it also attempts to provide sanitization of arguments
presented to gnupg, in order to avoid potential vulnerabilities.

@authors: A.M. Kuchling
          Steve Traugott
          Vinay Sajip
          Isis Lovecruft, <isis@leap.se> 0x2cdb8b35

Steve Traugott's documentation:
-------------------------------
    Portions of this module are derived from A.M. Kuchling's well-designed
    GPG.py, using Richard Jones' updated version 1.3, which can be found in
    the pycrypto CVS repository on Sourceforge:

    http://pycrypto.cvs.sourceforge.net/viewvc/pycrypto/gpg/GPG.py

    This module is *not* forward-compatible with amk's; some of the old
    interface has changed.  For instance, since I've added decrypt
    functionality, I elected to initialize with a 'gpghome' argument instead
    of 'keyring', so that gpg can find both the public and secret keyrings.
    I've also altered some of the returned objects in order for the caller to
    not have to know as much about the internals of the result classes.

    While the rest of ISconf is released under the GPL, I am releasing this
    single file under the same terms that A.M. Kuchling used for pycrypto.

    Steve Traugott, stevegt@terraluna.org
    Thu Jun 23 21:27:20 PDT 2005

Vinay Sajip's documentation:
----------------------------
    This version of the module has been modified from Steve Traugott's version
    (see http://trac.t7a.org/isconf/browser/trunk/lib/python/isconf/GPG.py) by
    Vinay Sajip to make use of the subprocess module (Steve's version uses
    os.fork() and so does not work on Windows). Renamed to gnupg.py to avoid
    confusion with the previous versions.

    A unittest harness (test_gnupg.py) has also been added.

    Modifications Copyright (C) 2008-2012 Vinay Sajip. All rights reserved.
"""

__module__ = 'gnupg'
__version__ = "0.3.1"
__author__ = "Isis Agora Lovecruft"
__date__  = "12 Febuary 2013"

import locale

try:
    from io import StringIO
    from io import BytesIO
except ImportError:
    from cStringIO import StringIO

import codecs
import locale
import logging
import os
import re
import socket
from subprocess import Popen
from subprocess import PIPE
import sys
import tempfile
import threading

try:
    import logging.NullHandler as NullHandler
except ImportError:
    class NullHandler(logging.Handler):
        def handle(self, record):
            pass
try:
    unicode
    _py3k = False
except NameError:
    _py3k = True


ESCAPE_PATTERN = re.compile(r'\\x([0-9a-f][0-9a-f])', re.I)

logger = logging.getLogger(__module__)
if not logger.handlers:
    logger.addHandler(NullHandler())


class ProtectedOption(Exception):
    """Raised when the option passed to GPG is disallowed."""

class UsageError(Exception):
    """Raised when you're Doing It Wrong."""


def _copy_data(instream, outstream):
    """
    Copy data from one stream to another.

    @param instream: A file descriptor to read from.
    @param outstream: A file descriptor to write to.
    """
    sent = 0

    try:
        assert isinstance(instream, BytesIO), "instream is not a file"
        assert isinstance(outstream, file), "outstream is not a file"
    except AssertionError as ae:
        logger.exception(ae)
        return

    if hasattr(sys.stdin, 'encoding'):
        enc = sys.stdin.encoding
    else:
        enc = 'ascii'

    while True:
        data = instream.read(1024)
        if len(data) == 0:
            break
        sent += len(data)
        logger.debug("sending chunk (%d): %r", sent, data[:256])
        try:
            outstream.write(data)
        except UnicodeError:
            try:
                outstream.write(data.encode(enc))
            except IOError:
                logger.exception('Error sending data: Broken pipe')
                break
        except IOError:
            # Can sometimes get 'broken pipe' errors even when the
            # data has all been sent
            logger.exception('Error sending data: Broken pipe')
            break
    try:
        outstream.close()
    except IOError:
        logger.exception('Got IOError while trying to close FD outstream')
    else:
        logger.debug("closed output, %d bytes sent", sent)

def _fix_unsafe(input):
    """
    Find characters used to escape from a string into a shell, and wrap them
    in quotes if they exist. Regex pilfered from python-3.x shlex module.

    @param input: The input intended for the gnupg process.
    """
    ## xxx do we want to add ';'?
    _unsafe = re.compile(r'[^\w@%+=:,./-]', 256)
    try:
        if len(_unsafe.findall(input)) == 0:
            return input
        else:
            clean = "'" + input.replace("'", "'\"'\"'") + "'"
            return clean
    except TypeError:
        return None

def _has_readwrite(path):
    """
    Determine if the real uid/gid of the executing user has read and write
    permissions for a directory or a file.

    @type path: C{str}
    @param path: The path to the directory or file to check permissions for.

    @rtype: C{bool}
    @param: True if real uid/gid has read+write permissions, False otherwise.
    """
    return os.access(path, os.R_OK and os.W_OK)

def _hyphenate(input, add_prefix=False):
    """
    Change underscores to hyphens so that object attributes can be easily
    tranlated to GPG option names.

    @type input: C{str}
    @param input: The attribute to hyphenate.

    @type add_prefix: C{bool}
    @param add_prefix: If True, add leading hyphens to the input.

    @rtype: C{str}
    @return: The :param:input with underscores changed to hyphens.
    """
    ret  = '--' if add_prefix else ''
    ret += input.replace('_', '-')
    return ret

def _is_allowed(input):
    """
    Check that an option or argument given to GPG is in the set of allowed
    options, the latter being a strict subset of the set of all options known
    to GPG.

    @type input: C{str}
    @param input: An input meant to be parsed as an option or flag to the GnuPG
                  process. Should be formatted the same as an option or flag
                  to the commandline gpg, i.e. "--encrypt-files".

    @type _possible: C{frozenset}
    @ivar _possible: All known GPG options and flags.

    @type _allowed: C{frozenset}
    @ivar _allowed: All allowed GPG options and flags, e.g. all GPG options and
                    flags which we are willing to acknowledge and parse. If we
                    want to support a new option, it will need to have its own
                    parsing class and its name will need to be added to this
                    set.

    @rtype: C{Exception} or C{str}
    @raise: UsageError if :ivar:_allowed is not a subset of :ivar:_possible.
            ProtectedOption if :param:input is not in the set :ivar:_allowed.
    @return: The original parameter :param:input, unmodified and unsanitized,
             if no errors occur.
    """

    _all = ("""
--allow-freeform-uid              --multifile
--allow-multiple-messages         --no
--allow-multisig-verification     --no-allow-freeform-uid
--allow-non-selfsigned-uid        --no-allow-multiple-messages
--allow-secret-key-import         --no-allow-non-selfsigned-uid
--always-trust                    --no-armor
--armor                           --no-armour
--armour                          --no-ask-cert-expire
--ask-cert-expire                 --no-ask-cert-level
--ask-cert-level                  --no-ask-sig-expire
--ask-sig-expire                  --no-auto-check-trustdb
--attribute-fd                    --no-auto-key-locate
--attribute-file                  --no-auto-key-retrieve
--auto-check-trustdb              --no-batch
--auto-key-locate                 --no-comments
--auto-key-retrieve               --no-default-keyring
--batch                           --no-default-recipient
--bzip2-compress-level            --no-disable-mdc
--bzip2-decompress-lowmem         --no-emit-version
--card-edit                       --no-encrypt-to
--card-status                     --no-escape-from-lines
--cert-digest-algo                --no-expensive-trust-checks
--cert-notation                   --no-expert
--cert-policy-url                 --no-force-mdc
--change-pin                      --no-force-v3-sigs
--charset                         --no-force-v4-certs
--check-sig                       --no-for-your-eyes-only
--check-sigs                      --no-greeting
--check-trustdb                   --no-groups
--cipher-algo                     --no-literal
--clearsign                       --no-mangle-dos-filenames
--command-fd                      --no-mdc-warning
--command-file                    --no-options
--comment                         --no-permission-warning
--completes-needed                --no-pgp2
--compress-algo                   --no-pgp6
--compression-algo                --no-pgp7
--compress-keys                   --no-pgp8
--compress-level                  --no-random-seed-file
--compress-sigs                   --no-require-backsigs
--ctapi-driver                    --no-require-cross-certification
--dearmor                         --no-require-secmem
--dearmour                        --no-rfc2440-text
--debug                           --no-secmem-warning
--debug-all                       --no-show-notation
--debug-ccid-driver               --no-show-photos
--debug-level                     --no-show-policy-url
--decrypt                         --no-sig-cache
--decrypt-files                   --no-sig-create-check
--default-cert-check-level        --no-sk-comments
--default-cert-expire             --no-strict
--default-cert-level              --notation-data
--default-comment                 --not-dash-escaped
--default-key                     --no-textmode
--default-keyserver-url           --no-throw-keyid
--default-preference-list         --no-throw-keyids
--default-recipient               --no-tty
--default-recipient-self          --no-use-agent
--default-sig-expire              --no-use-embedded-filename
--delete-keys                     --no-utf8-strings
--delete-secret-and-public-keys   --no-verbose
--delete-secret-keys              --no-version
--desig-revoke                    --openpgp
--detach-sign                     --options
--digest-algo                     --output
--disable-ccid                    --override-session-key
--disable-cipher-algo             --passphrase
--disable-dsa2                    --passphrase-fd
--disable-mdc                     --passphrase-file
--disable-pubkey-algo             --passphrase-repeat
--display                         --pcsc-driver
--display-charset                 --personal-cipher-preferences
--dry-run                         --personal-cipher-prefs
--dump-options                    --personal-compress-preferences
--edit-key                        --personal-compress-prefs
--emit-version                    --personal-digest-preferences
--enable-dsa2                     --personal-digest-prefs
--enable-progress-filter          --pgp2
--enable-special-filenames        --pgp6
--enarmor                         --pgp7
--enarmour                        --pgp8
--encrypt                         --photo-viewer
--encrypt-files                   --pipemode
--encrypt-to                      --preserve-permissions
--escape-from-lines               --primary-keyring
--exec-path                       --print-md
--exit-on-status-write-error      --print-mds
--expert                          --quick-random
--export                          --quiet
--export-options                  --reader-port
--export-ownertrust               --rebuild-keydb-caches
--export-secret-keys              --recipient
--export-secret-subkeys           --recv-keys
--fast-import                     --refresh-keys
--fast-list-mode                  --remote-user
--fetch-keys                      --require-backsigs
--fingerprint                     --require-cross-certification
--fixed-list-mode                 --require-secmem
--fix-trustdb                     --rfc1991
--force-mdc                       --rfc2440
--force-ownertrust                --rfc2440-text
--force-v3-sigs                   --rfc4880
--force-v4-certs                  --run-as-shm-coprocess
--for-your-eyes-only              --s2k-cipher-algo
--gen-key                         --s2k-count
--gen-prime                       --s2k-digest-algo
--gen-random                      --s2k-mode
--gen-revoke                      --search-keys
--gnupg                           --secret-keyring
--gpg-agent-info                  --send-keys
--gpgconf-list                    --set-filename
--gpgconf-test                    --set-filesize
--group                           --set-notation
--help                            --set-policy-url
--hidden-encrypt-to               --show-keyring
--hidden-recipient                --show-notation
--homedir                         --show-photos
--honor-http-proxy                --show-policy-url
--ignore-crc-error                --show-session-key
--ignore-mdc-error                --sig-keyserver-url
--ignore-time-conflict            --sign
--ignore-valid-from               --sign-key
--import                          --sig-notation
--import-options                  --sign-with
--import-ownertrust               --sig-policy-url
--interactive                     --simple-sk-checksum
--keyid-format                    --sk-comments
--keyring                         --skip-verify
--keyserver                       --status-fd
--keyserver-options               --status-file
--lc-ctype                        --store
--lc-messages                     --strict
--limit-card-insert-tries         --symmetric
--list-config                     --temp-directory
--list-key                        --textmode
--list-keys                       --throw-keyid
--list-only                       --throw-keyids
--list-options                    --trustdb-name
--list-ownertrust                 --trusted-key
--list-packets                    --trust-model
--list-public-keys                --try-all-secrets
--list-secret-keys                --ttyname
--list-sig                        --ttytype
--list-sigs                       --ungroup
--list-trustdb                    --update-trustdb
--load-extension                  --use-agent
--local-user                      --use-embedded-filename
--lock-multiple                   --user
--lock-never                      --utf8-strings
--lock-once                       --verbose
--logger-fd                       --verify
--logger-file                     --verify-files
--lsign-key                       --verify-options
--mangle-dos-filenames            --version
--marginals-needed                --warranty
--max-cert-depth                  --with-colons
--max-output                      --with-fingerprint
--merge-only                      --with-key-data
--min-cert-level                  --yes
""").split()

    _possible = frozenset(_all)

    ## these are the allowed options we will handle so far, all others should
    ## be dropped. this dance is so that when new options are added later, we
    ## merely add the to the _allowed list, and the `` _allowed.issubset``
    ## assertion will check that GPG will recognise them
    ##
    ## xxx key fetching/retrieving options: [fetch_keys, merge_only, recv_keys]
    ##
    ## xxx which ones do we want as defaults?
    ##     eg, --no-show-photos would mitigate things like
    ##     https://www-01.ibm.com/support/docview.wss?uid=swg21620982
    _allowed = frozenset(
        ['--list-packets', '--delete-keys', '--delete-secret-keys',
         '--encrypt', '--print-mds', '--print-md', '--sign',
         '--encrypt-files', '--gen-key', '--decrypt', '--decrypt-files',
         '--list-keys', '--import', '--verify', '--version',
         '--status-fd', '--no-tty', '--homedir', '--no-default-keyring',
         '--keyring', '--passphrase-fd', '--fingerprint', '--with-colons'])

    ## check that _allowed is a subset of _possible
    try:
        assert _allowed.issubset(_possible), \
            '_allowed is not subset of known options, difference: %s' \
            % _allowed.difference(_possible)
    except AssertionError as ae:   ## 'as' syntax requires python>=2.6
        logger.debug("gnupg._is_allowed(): %s" % ae.message)
        raise UsageError(ae.message)

    ## if we got a list of args, join them
    if not isinstance(input, str):
        input = ' '.join([x for x in input])

    if isinstance(input, str):
        if input.find('_') > 0:
            if not input.startswith('--'):
                hyphenated = _hyphenate(input, add_prefix=True)
            else:
                hyphenated = _hyphenate(input)
        else:
            hyphenated = input
            try:
                assert hyphenated in _allowed
            except AssertionError as ae:
                logger.warn("Dropping option '%s'..."
                            % _fix_unsafe(hyphenated))
                raise ProtectedOption("Option '%s' not supported."
                                      % _fix_unsafe(hyphenated))
            else:
                logger.debug("Got allowed option '%s'."
                             % _fix_unsafe(hyphenated))
                return input
    return None

def _is_file(input):
    """
    Check that the size of the thing which is supposed to be a filename has
    size greater than zero, without following symbolic links or using
    :func:`os.path.isfile`.
    """
    try:
        assert os.lstat(input).st_size > 0, "not a file: %s" % input
    except (AssertionError, TypeError) as error:
        logger.debug(error.message)
        return False
    else:
        return True

def _is_sequence(instance):
    return isinstance(instance,list) or isinstance(instance,tuple)

def _make_binary_stream(s, encoding):
    try:
        if _py3k:
            if isinstance(s, str):
                s = s.encode(encoding)
        else:
            if type(s) is not str:
                s = s.encode(encoding)
        from io import BytesIO
        rv = BytesIO(s)
    except ImportError:
        rv = StringIO(s)
    return rv

def _sanitise(*args):
    """
    Take an arg or the key portion of a kwarg and check that it is in the set
    of allowed GPG options and flags, and that it has the correct type. Then,
    attempt to escape any unsafe characters. If an option is not allowed,
    drop it with a logged warning. Returns a dictionary of all sanitised,
    allowed options.

    Each new option that we support that is not a boolean, but instead has
    some extra inputs, i.e. "--encrypt-file foo.txt", will need some basic
    safety checks added here.

    GnuPG has three-hundred and eighteen commandline flags. Also, not all
    implementations of OpenPGP parse PGP packets and headers in the same way,
    so there is added potential there for messing with calls to GPG.

    For information on the PGP message format specification, see:
        https://www.ietf.org/rfc/rfc1991.txt

    If you're asking, "Is this *really* necessary?": No. Not really. See:
        https://xkcd.com/1181/

    @type args: C{str}
    @param args: (optional) The boolean arguments which will be passed to the
                 GnuPG process.
    @rtype: C{str}
    @param: :ivar:sanitised
    """

    def _check_arg_and_value(arg, value):
        """
        Check that a single :param:arg is an allowed option. If it is allowed,
        quote out any escape characters in :param:values, and add the pair to
        :ivar:sanitised.

        @type arg: C{str}

        @param arg: The arguments which will be passed to the GnuPG process,
                    and, optionally their corresponding values.  The values are
                    any additional arguments following the GnuPG option or
                    flag. For example, if we wanted to pass "--encrypt
                    --recipient isis@leap.se" to gpg, then "--encrypt" would be
                    an arg without a value, and "--recipient" would also be an
                    arg, with a value of "isis@leap.se".
        @type sanitised: C{str}
        @ivar sanitised: The sanitised, allowed options.
        """
        safe_values = str()

        try:
            allowed_flag = _is_allowed(arg)
            assert allowed_flag is not None, \
                "_check_arg_and_value(): got None for allowed_flag"
        except (AssertionError, ProtectedOption) as error:
            logger.warn(error.message)
            logger.debug("Dropping option '%s'..." % _fix_unsafe(arg))
        else:
            safe_values += (allowed_flag + " ")
            if isinstance(value, str):
                value_list = []
                if value.find(' ') > 0:
                    value_list = value.split(' ')
                else:
                    logger.debug("_check_values(): got non-string for values")
                for value in value_list:
                    safe_value = _fix_unsafe(value)
                    if allowed_flag == '--encrypt' or '--encrypt-files' \
                            or '--decrypt' or '--decrypt-file' \
                            or '--import' or '--verify':
                        ## xxx what other things should we check for?
                        ## Place checks here:
                        if _is_file(safe_value):
                            safe_values += (safe_value + " ")
                        else:
                            logger.debug("Got non-filename for %s option: %s"
                                         % (allowed_flag, safe_value))
                    else:
                        safe_values += (safe_value + " ")
                        logger.debug("Got non-checked value: %s" % safe_value)
        return safe_values

    checked = []

    if args is not None:
        for arg in args:
            if isinstance(arg, str):
                logger.debug("_sanitise(): Got arg string: %s" % arg)
                ## if we're given a string with a bunch of options in it split
                ## them up and deal with them separately
                if arg.find(' ') > 0:
                    filo = arg.split()
                    filo.reverse()
                    is_flag = lambda x: x.startswith('-')
                    new_arg, new_value = str(), str()
                    while len(filo) > 0:
                        if is_flag(filo[0]):
                            new_arg = filo.pop()
                            if len(filo) > 0:
                                while not is_flag(filo[0]):
                                    new_value += (filo.pop() + ' ')
                        else:
                            logger.debug("Got non-flag argument: %s" % filo[0])
                            filo.pop()
                        safe = _check_arg_and_value(new_arg, new_value)
                        if safe is not None and safe.strip() != '':
                            logger.debug("_sanitise(): appending args: %s" % safe)
                            checked.append(safe)
                else:
                    safe = _check_arg_and_value(arg, None)
                    logger.debug("_sanitise(): appending args: %s" % safe)
                    checked.append(safe)
            elif isinstance(arg, list): ## happens with '--version'
                logger.debug("_sanitise(): Got arg list: %s" % arg)
                for a in arg:
                    if a.startswith('--'):
                        safe = _check_arg_and_value(a, None)
                        logger.debug("_sanitise(): appending args: %s" % safe)
                        checked.append(safe)
            else:
                logger.debug("_sanitise(): got non string or list arg: %s" % arg)

    sanitised = ' '.join(x for x in checked)
    return sanitised

def _sanitise_list(arg_list):
    """
    A generator for running through a list of gpg options and sanitising them.

    @type arg_list: C{list}
    @param arg_list: A list of options and flags for gpg.
    @rtype: C{generator}
    @return: A generator whose next() method returns each of the items in
             :param:arg_list after calling :func:_sanitise with that item as a
             parameter.
    """
    if isinstance(arg_list, list):
        for arg in arg_list:
            safe_arg = _sanitise(arg)
            if safe_arg != "":
                yield safe_arg

def _threaded_copy_data(instream, outstream):
    wr = threading.Thread(target=_copy_data, args=(instream, outstream))
    wr.setDaemon(True)
    logger.debug('data copier: %r, %r, %r', wr, instream, outstream)
    wr.start()
    return wr

def _underscore(input, remove_prefix=False):
    """
    Change hyphens to underscores so that GPG option names can be easily
    tranlated to object attributes.

    @type input: C{str}
    @param input: The input intended for the gnupg process.

    @type remove_prefix: C{bool}
    @param remove_prefix: If True, strip leading hyphens from the input.

    @rtype: C{str}
    @return: The :param:input with hyphens changed to underscores.
    """
    if not remove_prefix:
        return input.replace('-', '_')
    else:
        return input.lstrip('-').replace('-', '_')

def _which(executable, flags=os.X_OK):
    """Borrowed from Twisted's :mod:twisted.python.proutils .

    Search PATH for executable files with the given name.

    On newer versions of MS-Windows, the PATHEXT environment variable will be
    set to the list of file extensions for files considered executable. This
    will normally include things like ".EXE". This fuction will also find files
    with the given name ending with any of these extensions.

    On MS-Windows the only flag that has any meaning is os.F_OK. Any other
    flags will be ignored.

    Note: This function does not help us prevent an attacker who can already
    manipulate the environment's PATH settings from placing malicious code
    higher in the PATH. It also does happily follows links.

    @type name: C{str}
    @param name: The name for which to search.
    @type flags: C{int}
    @param flags: Arguments to L{os.access}.
    @rtype: C{list}
    @param: A list of the full paths to files found, in the order in which
            they were found.
    """
    result = []
    exts = filter(None, os.environ.get('PATHEXT', '').split(os.pathsep))
    path = os.environ.get('PATH', None)
    if path is None:
        return []
    for p in os.environ.get('PATH', '').split(os.pathsep):
        p = os.path.join(p, executable)
        if os.access(p, flags):
            result.append(p)
        for e in exts:
            pext = p + e
            if os.access(pext, flags):
                result.append(pext)
    return result

def _write_passphrase(stream, passphrase, encoding):
    passphrase = '%s\n' % passphrase
    passphrase = passphrase.encode(encoding)
    stream.write(passphrase)
    logger.debug("Wrote passphrase.")


class Verify(object):
    """Handle status messages for --verify"""

    TRUST_UNDEFINED = 0
    TRUST_NEVER = 1
    TRUST_MARGINAL = 2
    TRUST_FULLY = 3
    TRUST_ULTIMATE = 4

    TRUST_LEVELS = {
        "TRUST_UNDEFINED" : TRUST_UNDEFINED,
        "TRUST_NEVER" : TRUST_NEVER,
        "TRUST_MARGINAL" : TRUST_MARGINAL,
        "TRUST_FULLY" : TRUST_FULLY,
        "TRUST_ULTIMATE" : TRUST_ULTIMATE,
    }

    def __init__(self, gpg):
        self.gpg = gpg
        self.valid = False
        self.fingerprint = self.creation_date = self.timestamp = None
        self.signature_id = self.key_id = None
        self.username = None
        self.status = None
        self.pubkey_fingerprint = None
        self.expire_timestamp = None
        self.sig_timestamp = None
        self.trust_text = None
        self.trust_level = None

    def __nonzero__(self):
        return self.valid

    __bool__ = __nonzero__

    def handle_status(self, key, value):
        if key in self.TRUST_LEVELS:
            self.trust_text = key
            self.trust_level = self.TRUST_LEVELS[key]
        elif key in ("RSA_OR_IDEA", "NODATA", "IMPORT_RES", "PLAINTEXT",
                   "PLAINTEXT_LENGTH", "POLICY_URL", "DECRYPTION_INFO",
                   "DECRYPTION_OKAY", "INV_SGNR"):
            pass
        elif key == "BADSIG":
            self.valid = False
            self.status = 'signature bad'
            self.key_id, self.username = value.split(None, 1)
        elif key == "GOODSIG":
            self.valid = True
            self.status = 'signature good'
            self.key_id, self.username = value.split(None, 1)
        elif key == "VALIDSIG":
            (self.fingerprint,
             self.creation_date,
             self.sig_timestamp,
             self.expire_timestamp) = value.split()[:4]
            # may be different if signature is made with a subkey
            self.pubkey_fingerprint = value.split()[-1]
            self.status = 'signature valid'
        elif key == "SIG_ID":
            (self.signature_id,
             self.creation_date, self.timestamp) = value.split()
        elif key == "ERRSIG":
            self.valid = False
            (self.key_id,
             algo, hash_algo,
             cls,
             self.timestamp) = value.split()[:5]
            self.status = 'signature error'
        elif key == "DECRYPTION_FAILED":
            self.valid = False
            self.key_id = value
            self.status = 'decryption failed'
        elif key == "NO_PUBKEY":
            self.valid = False
            self.key_id = value
            self.status = 'no public key'
        elif key in ("KEYEXPIRED", "SIGEXPIRED"):
            # these are useless in verify, since they are spit out for any
            # pub/subkeys on the key, not just the one doing the signing.
            # if we want to check for signatures with expired key,
            # the relevant flag is EXPKEYSIG.
            pass
        elif key in ("EXPKEYSIG", "REVKEYSIG"):
            # signed with expired or revoked key
            self.valid = False
            self.key_id = value.split()[0]
            self.status = (('%s %s') % (key[:3], key[3:])).lower()
        else:
            raise ValueError("Unknown status message: %r" % key)

class ImportResult(object):
    """Handle status messages for --import"""

    counts = '''count no_user_id imported imported_rsa unchanged
            n_uids n_subk n_sigs n_revoc sec_read sec_imported
            sec_dups not_imported'''.split()
    def __init__(self, gpg):
        self.gpg = gpg
        self.imported = []
        self.results = []
        self.fingerprints = []
        for result in self.counts:
            setattr(self, result, None)

    def __nonzero__(self):
        if self.not_imported: return False
        if not self.fingerprints: return False
        return True

    __bool__ = __nonzero__

    ok_reason = {
        '0': 'Not actually changed',
        '1': 'Entirely new key',
        '2': 'New user IDs',
        '4': 'New signatures',
        '8': 'New subkeys',
        '16': 'Contains private key',
    }

    problem_reason = {
        '0': 'No specific reason given',
        '1': 'Invalid Certificate',
        '2': 'Issuer Certificate missing',
        '3': 'Certificate Chain too long',
        '4': 'Error storing certificate',
    }

    def handle_status(self, key, value):
        if key == "IMPORTED":
            # this duplicates info we already see in import_ok & import_problem
            pass
        elif key == "NODATA":
            self.results.append({'fingerprint': None,
                'problem': '0', 'text': 'No valid data found'})
        elif key == "IMPORT_OK":
            reason, fingerprint = value.split()
            reasons = []
            for code, text in list(self.ok_reason.items()):
                if int(reason) | int(code) == int(reason):
                    reasons.append(text)
            reasontext = '\n'.join(reasons) + "\n"
            self.results.append({'fingerprint': fingerprint,
                'ok': reason, 'text': reasontext})
            self.fingerprints.append(fingerprint)
        elif key == "IMPORT_PROBLEM":
            try:
                reason, fingerprint = value.split()
            except:
                reason = value
                fingerprint = '<unknown>'
            self.results.append({'fingerprint': fingerprint,
                'problem': reason, 'text': self.problem_reason[reason]})
        elif key == "IMPORT_RES":
            import_res = value.split()
            for i in range(len(self.counts)):
                setattr(self, self.counts[i], int(import_res[i]))
        elif key == "KEYEXPIRED":
            self.results.append({'fingerprint': None,
                'problem': '0', 'text': 'Key expired'})
        elif key == "SIGEXPIRED":
            self.results.append({'fingerprint': None,
                'problem': '0', 'text': 'Signature expired'})
        else:
            raise ValueError("Unknown status message: %r" % key)

    def summary(self):
        l = []
        l.append('%d imported' % self.imported)
        if self.not_imported:
            l.append('%d not imported' % self.not_imported)
        return ', '.join(l)

class ListKeys(list):
    """ Handle status messages for --list-keys.

        Handle pub and uid (relating the latter to the former).

        Don't care about (info from src/DETAILS):

        crt = X.509 certificate
        crs = X.509 certificate and private key available
        ssb = secret subkey (secondary key)
        uat = user attribute (same as user id except for field 10).
        sig = signature
        rev = revocation signature
        pkd = public key data (special field format, see below)
        grp = reserved for gpgsm
        rvk = revocation key
    """

    def __init__(self, gpg):
        super(ListKeys, self).__init__()
        self.gpg = gpg
        self.curkey = None
        self.fingerprints = []
        self.uids = []

    def key(self, args):
        vars = ("""
            type trust length algo keyid date expires dummy ownertrust uid
        """).split()
        self.curkey = {}
        for i in range(len(vars)):
            self.curkey[vars[i]] = args[i]
        self.curkey['uids'] = []
        if self.curkey['uid']:
            self.curkey['uids'].append(self.curkey['uid'])
        del self.curkey['uid']
        self.curkey['subkeys'] = []
        self.append(self.curkey)

    pub = sec = key

    def fpr(self, args):
        self.curkey['fingerprint'] = args[9]
        self.fingerprints.append(args[9])

    def uid(self, args):
        uid = args[9]
        uid = ESCAPE_PATTERN.sub(lambda m: chr(int(m.group(1), 16)), uid)
        self.curkey['uids'].append(uid)
        self.uids.append(uid)

    def sub(self, args):
        subkey = [args[4], args[11]]
        self.curkey['subkeys'].append(subkey)

    def handle_status(self, key, value):
        pass

class Crypt(Verify):
    """Handle status messages for --encrypt and --decrypt"""
    def __init__(self, gpg):
        Verify.__init__(self, gpg)
        self.data = ''
        self.ok = False
        self.status = ''

    def __nonzero__(self):
        if self.ok: return True
        return False

    __bool__ = __nonzero__

    def __str__(self):
        return self.data.decode(self.gpg.encoding, self.gpg.decode_errors)

    def handle_status(self, key, value):
        if key in ("ENC_TO", "USERID_HINT", "GOODMDC", "END_DECRYPTION",
                   "BEGIN_SIGNING", "NO_SECKEY", "ERROR", "NODATA",
                   "CARDCTRL"):
            # in the case of ERROR, this is because a more specific error
            # message will have come first
            pass
        elif key in ("NEED_PASSPHRASE", "BAD_PASSPHRASE", "GOOD_PASSPHRASE",
                     "MISSING_PASSPHRASE", "DECRYPTION_FAILED",
                     "KEY_NOT_CREATED"):
            self.status = key.replace("_", " ").lower()
        elif key == "NEED_PASSPHRASE_SYM":
            self.status = 'need symmetric passphrase'
        elif key == "BEGIN_DECRYPTION":
            self.status = 'decryption incomplete'
        elif key == "BEGIN_ENCRYPTION":
            self.status = 'encryption incomplete'
        elif key == "DECRYPTION_OKAY":
            self.status = 'decryption ok'
            self.ok = True
        elif key == "END_ENCRYPTION":
            self.status = 'encryption ok'
            self.ok = True
        elif key == "INV_RECP":
            self.status = 'invalid recipient'
        elif key == "KEYEXPIRED":
            self.status = 'key expired'
        elif key == "SIG_CREATED":
            self.status = 'sig created'
        elif key == "SIGEXPIRED":
            self.status = 'sig expired'
        else:
            Verify.handle_status(self, key, value)

class GenKey(object):
    """Handle status messages for --gen-key"""
    def __init__(self, gpg):
        self.gpg = gpg
        self.type = None
        self.fingerprint = None

    def __nonzero__(self):
        if self.fingerprint: return True
        return False

    __bool__ = __nonzero__

    def __str__(self):
        return self.fingerprint or ''

    def handle_status(self, key, value):
        if key in ("PROGRESS", "GOOD_PASSPHRASE", "NODATA", "KEY_NOT_CREATED"):
            pass
        elif key == "KEY_CREATED":
            (self.type, self.fingerprint) = value.split()
        else:
            raise ValueError("Unknown status message: %r" % key)

class DeleteResult(object):
    """Handle status messages for --delete-key and --delete-secret-key"""
    def __init__(self, gpg):
        self.gpg = gpg
        self.status = 'ok'

    def __str__(self):
        return self.status

    problem_reason = {
        '1': 'No such key',
        '2': 'Must delete secret key first',
        '3': 'Ambigious specification',
        }

    def handle_status(self, key, value):
        if key == "DELETE_PROBLEM":
            self.status = self.problem_reason.get(value, "Unknown error: %r"
                                                  % value)
        else:
            raise ValueError("Unknown status message: %r" % key)

class Sign(object):
    """Handle status messages for --sign"""
    def __init__(self, gpg):
        self.gpg = gpg
        self.type = None
        self.fingerprint = None

    def __nonzero__(self):
        return self.fingerprint is not None

    __bool__ = __nonzero__

    def __str__(self):
        return self.data.decode(self.gpg.encoding, self.gpg.decode_errors)

    def handle_status(self, key, value):
        if key in ("USERID_HINT", "NEED_PASSPHRASE", "BAD_PASSPHRASE",
                   "GOOD_PASSPHRASE", "BEGIN_SIGNING", "CARDCTRL",
                   "INV_SGNR", "NODATA"):
            pass
        elif key == "SIG_CREATED":
            (self.type, algo, hashalgo, cls, self.timestamp,
             self.fingerprint) = value.split()
        else:
            raise ValueError("Unknown status message: %r" % key)

class GPG(object):
    """Encapsulate access to the gpg executable"""
    decode_errors = 'strict'

    result_map = {'crypt': Crypt,
                  'delete': DeleteResult,
                  'generate': GenKey,
                  'import': ImportResult,
                  'list': ListKeys,
                  'sign': Sign,
                  'verify': Verify,}

    def __init__(self, gpgbinary=None, gpghome=None,
                 verbose=False, use_agent=False,
                 keyring=None, secring=None, pubring=None,
                 options=None):
        """
        Initialize a GnuPG process wrapper.

        @type gpgbinary: C{str}
        @param gpgbinary: Name for GnuPG binary executable. If the absolute
                            path is not given, the evironment variable $PATH is
                            searched for the executable and checked that the
                            real uid/gid of the user has sufficient permissions.
        @type gpghome: C{str}
        @param gpghome: Full pathname to directory containing the public and
                        private keyrings. Default is whatever GnuPG defaults
                        to.

        @type keyring: C{str}
        @param keyring: raises C{DeprecationWarning}. Use :param:secring.

        @type secring: C{str}
        @param secring: Name of alternative secret keyring file to use. If left
                        unspecified, this will default to using 'secring.gpg'
                        in the :param:gpghome directory, and create that file
                        if it does not exist.

        @type pubring: C{str}
        @param pubring: Name of alternative public keyring file to use. If left
                        unspecified, this will default to using 'pubring.gpg'
                        in the :param:gpghome directory, and create that file
                        if it does not exist.

        @options: A list of additional options to pass to the GPG binary.

        @rtype: C{Exception} or C{}
        @raises: RuntimeError with explanation message if there is a problem
                 invoking gpg.
        @returns:
        """

        if not gpghome:
            gpghome = os.path.join(os.getcwd(), 'gnupg')
        self.gpghome = _fix_unsafe(gpghome)
        if self.gpghome:
            if not os.path.isdir(self.gpghome):
                message = ("Creating gpg home dir: %s" % gpghome)
                logger.debug("GPG.__init__(): %s" % message)
                os.makedirs(self.gpghome, 0x1C0)
            if not os.path.isabs(self.gpghome):
                message = ("Got non-abs gpg home dir path: %s" % self.gpghome)
                logger.debug("GPG.__init__(): %s" % message)
                self.gpghome = os.path.abspath(self.gpghome)
        else:
            message = ("Unsuitable gpg home dir: %s" % gpghome)
            logger.debug("GPG.__init__(): %s" % message)

        ## find the absolute path, check that it is not a link, and check that
        ## we have exec permissions
        bin = None
        if gpgbinary is not None:
            if not os.path.isabs(gpgbinary):
                try: bin = _which(gpgbinary)[0]
                except IndexError as ie: logger.debug(ie.message)
        if bin is None:
            try: bin = _which('gpg')[0]
            except IndexError: raise RuntimeError("gpg is not installed")
        try:
            assert os.path.isabs(bin), "Path to gpg binary not absolute"
            assert not os.path.islink(bin), "Path to gpg binary is symbolic link"
            assert os.access(bin, os.X_OK), "Lacking +x perms for gpg binary"
        except (AssertionError, AttributeError) as ae:
            logger.debug("GPG.__init__(): %s" % ae.message)
        else:
            self.gpgbinary = bin

        if keyring is not None:
            try:
                raise DeprecationWarning(
                    "Option 'keyring' changing to 'secring'")
            except DeprecationWarning as dw:
                log.warn(dw.message)
            finally:
                secring = keyring

        secring = 'secring.gpg' if secring is None else _fix_unsafe(secring)
        pubring = 'pubring.gpg' if pubring is None else _fix_unsafe(pubring)

        self.secring = os.path.join(self.gpghome, secring)
        self.pubring = os.path.join(self.gpghome, pubring)
        ## XXX should eventually be changed throughout to 'secring', but until
        ## then let's not break backward compatibility
        self.keyring = self.secring

        for ring in [self.secring, self.pubring]:
            if ring and not os.path.isfile(ring):
                with open(ring, 'a+') as ringfile:
                    ringfile.write(" ")
                    ringfile.flush()
            try:
                assert _has_readwrite(ring), \
                    ("Need r+w for %s" % ring)
            except AssertionError as ae:
                logger.debug(ae.message)

        self.options = _sanitise(options) if options else None

        ## xxx TODO: hack the locale module away so we can use this on android
        self.encoding = locale.getpreferredencoding()
        if self.encoding is None: # This happens on Jython!
            self.encoding = sys.stdin.encoding

        try:
            assert self.gpghome is not None, "Got None for self.gpghome"
            assert _has_readwrite(self.gpghome), ("Home dir %s needs r+w"
                                                  % self.gpghome)
            assert self.gpgbinary, "Could not find gpgbinary %s" % full
            assert isinstance(verbose, bool), "'verbose' must be boolean"
            assert isinstance(use_agent, bool), "'use_agent' must be boolean"
            if self.options:
                assert isinstance(options, str), ("options not formatted: %s"
                                                  % options)
        except (AssertionError, AttributeError) as ae:
            logger.debug("GPG.__init__(): %s" % ae.message)
            raise RuntimeError(ae.message)
        else:
            self.verbose = verbose
            self.use_agent = use_agent

            proc = self._open_subprocess(["--version"])
            result = self.result_map['list'](self)
            self._collect_output(proc, result, stdin=proc.stdin)
            if proc.returncode != 0:
                raise RuntimeError("Error invoking gpg: %s: %s"
                                   % (proc.returncode, result.stderr))

    def make_args(self, args, passphrase=False):
        """
        Make a list of command line elements for GPG. The value of ``args``
        will be appended. The ``passphrase`` argument needs to be True if
        a passphrase will be sent to GPG, else False.
        """
        cmd = [self.gpgbinary, '--status-fd 2 --no-tty']
        if self.gpghome:
            cmd.append('--homedir "%s"' % self.gpghome)
        if self.keyring:
            cmd.append('--no-default-keyring --keyring "%s"' % self.keyring)
        if passphrase:
            cmd.append('--batch --passphrase-fd 0')
        if self.use_agent:
            cmd.append('--use-agent')
        if self.options:
            [cmd.append(opt) for opt in iter(_sanitise_list(self.options))]
        if args:
            [cmd.append(arg) for arg in iter(_sanitise_list(args))]
        logger.debug("make_args(): Using command: %s" % cmd)
        return cmd

    def _open_subprocess(self, args=None, passphrase=False):
        # Internal method: open a pipe to a GPG subprocess and return
        # the file objects for communicating with it.
        cmd = ' '.join(self.make_args(args, passphrase))
        if self.verbose:
            print(cmd)
        logger.debug("%s", cmd)
        return Popen(cmd, shell=True, stdin=PIPE, stdout=PIPE, stderr=PIPE)

    def _read_response(self, stream, result):
        # Internal method: reads all the stderr output from GPG, taking notice
        # only of lines that begin with the magic [GNUPG:] prefix.
        #
        # Calls methods on the response object for each valid token found,
        # with the arg being the remainder of the status line.
        lines = []
        while True:
            line = stream.readline()
            if len(line) == 0:
                break
            lines.append(line)
            line = line.rstrip()
            if self.verbose:
                print(line)
            logger.debug("%s", line)
            if line[0:9] == '[GNUPG:] ':
                # Chop off the prefix
                line = line[9:]
                L = line.split(None, 1)
                keyword = L[0]
                if len(L) > 1:
                    value = L[1]
                else:
                    value = ""
                result.handle_status(keyword, value)
        result.stderr = ''.join(lines)

    def _read_data(self, stream, result):
        # Read the contents of the file from GPG's stdout
        chunks = []
        while True:
            data = stream.read(1024)
            if len(data) == 0:
                break
            logger.debug("chunk: %r" % data[:256])
            chunks.append(data)
        if _py3k:
            # Join using b'' or '', as appropriate
            result.data = type(data)().join(chunks)
        else:
            result.data = ''.join(chunks)

    def _collect_output(self, process, result, writer=None, stdin=None):
        """
        Drain the subprocesses output streams, writing the collected output
        to the result. If a writer thread (writing to the subprocess) is given,
        make sure it's joined before returning. If a stdin stream is given,
        close it before returning.
        """
        stderr = codecs.getreader(self.encoding)(process.stderr)
        rr = threading.Thread(target=self._read_response, args=(stderr, result))
        rr.setDaemon(True)
        logger.debug('stderr reader: %r', rr)
        rr.start()

        stdout = process.stdout
        dr = threading.Thread(target=self._read_data, args=(stdout, result))
        dr.setDaemon(True)
        logger.debug('stdout reader: %r', dr)
        dr.start()

        dr.join()
        rr.join()
        if writer is not None:
            writer.join()
        process.wait()
        if stdin is not None:
            try:
                stdin.close()
            except IOError:
                pass
        stderr.close()
        stdout.close()

    def _handle_io(self, args, file, result, passphrase=None, binary=False):
        """
        Handle a call to GPG - pass input data, collect output data.
        """
        if passphrase is not None:
            ask_passphrase = True
        else:
            ask_passphrase = False
        p = self._open_subprocess(args, ask_passphrase)
        if not binary:
            stdin = codecs.getwriter(self.encoding)(p.stdin)
        else:
            stdin = p.stdin
        if passphrase:
            _write_passphrase(stdin, passphrase, self.encoding)
        writer = _threaded_copy_data(file, stdin)
        self._collect_output(p, result, writer, stdin)
        return result

    #
    # SIGNATURE METHODS
    #
    def sign(self, message, **kwargs):
        """sign message"""
        f = _make_binary_stream(message, self.encoding)
        result = self.sign_file(f, **kwargs)
        f.close()
        return result

    def sign_file(self, file, keyid=None, passphrase=None, clearsign=True,
                  detach=False, binary=False):
        """sign file"""
        logger.debug("sign_file: %s", file)
        if binary:
            args = ['-s']
        else:
            args = ['-sa']

        if clearsign:
            args.append("--clearsign")
            if detach:
                logger.debug(
                    "Cannot use --clearsign and --detach-sign simultaneously.")
                logger.debug(
                    "Using default GPG behaviour: --clearsign only.")
        elif detach and not clearsign:
            args.append("--detach-sign")

        if keyid:
            args.append('--default-key "%s"' % keyid)

        result = self.result_map['sign'](self)
        #We could use _handle_io here except for the fact that if the
        #passphrase is bad, gpg bails and you can't write the message.
        p = self._open_subprocess(args, passphrase is not None)
        try:
            stdin = p.stdin
            if passphrase:
                _write_passphrase(stdin, passphrase, self.encoding)
            writer = _threaded_copy_data(file, stdin)
        except IOError:
            logging.exception("error writing message")
            writer = None
        self._collect_output(p, result, writer, stdin)
        return result

    def verify(self, data):
        """Verify the signature on the contents of the string 'data'

        >>> gpg = GPG(gpghome="keys")
        >>> input = gpg.gen_key_input(Passphrase='foo')
        >>> key = gpg.gen_key(input)
        >>> assert key
        >>> sig = gpg.sign('hello',keyid=key.fingerprint,passphrase='bar')
        >>> assert not sig
        >>> sig = gpg.sign('hello',keyid=key.fingerprint,passphrase='foo')
        >>> assert sig
        >>> verify = gpg.verify(sig.data)
        >>> assert verify

        """
        f = _make_binary_stream(data, self.encoding)
        result = self.verify_file(f)
        f.close()
        return result

    def verify_file(self, file, data_filename=None):
        """
        Verify the signature on the contents of a file or file-like
        object. Can handle embedded signatures as well as detached
        signatures. If using detached signatures, the file containing the
        detached signature should be specified as the :param:`data_filename`.

        @param file: A file descriptor object. Its type will be checked with
                     :func:`_is_file`.
        @param data_filename: (optional) A file containing the GPG signature
                              data for :param:`file`. If given, :param:`file`
                              is verified via this detached signature.
        """
        ## attempt to wrap any escape characters in quotes:
        safe_file = _fix_unsafe(file)

        ## check that :param:`file` is actually a file:
        _is_file(safe_file)

        logger.debug('verify_file: %r, %r', safe_file, data_filename)
        result = self.result_map['verify'](self)
        args = ['--verify']
        if data_filename is None:
            self._handle_io(args, safe_file, result, binary=True)
        else:
            safe_data_filename = _fix_unsafe(data_filename)

            logger.debug('Handling detached verification')
            fd, fn = tempfile.mkstemp(prefix='pygpg')

            with open(safe_file) as sf:
                contents = sf.read()
                os.write(fd, s)
                os.close(fd)
                logger.debug('Wrote to temp file: %r', contents)
                args.append(fn)
                args.append('"%s"' % safe_data_filename)

                try:
                    p = self._open_subprocess(args)
                    self._collect_output(p, result, stdin=p.stdin)
                finally:
                    os.unlink(fn)

        return result

    #
    # KEY MANAGEMENT
    #
    def import_keys(self, key_data):
        """
        Import the key_data into our keyring.

        >>> import shutil
        >>> shutil.rmtree("keys")
        >>> gpg = GPG(gpghome="keys")
        >>> input = gpg.gen_key_input()
        >>> result = gpg.gen_key(input)
        >>> print1 = result.fingerprint
        >>> result = gpg.gen_key(input)
        >>> print2 = result.fingerprint
        >>> pubkey1 = gpg.export_keys(print1)
        >>> seckey1 = gpg.export_keys(print1,secret=True)
        >>> seckeys = gpg.list_keys(secret=True)
        >>> pubkeys = gpg.list_keys()
        >>> assert print1 in seckeys.fingerprints
        >>> assert print1 in pubkeys.fingerprints
        >>> str(gpg.delete_keys(print1))
        'Must delete secret key first'
        >>> str(gpg.delete_keys(print1,secret=True))
        'ok'
        >>> str(gpg.delete_keys(print1))
        'ok'
        >>> str(gpg.delete_keys("nosuchkey"))
        'No such key'
        >>> seckeys = gpg.list_keys(secret=True)
        >>> pubkeys = gpg.list_keys()
        >>> assert not print1 in seckeys.fingerprints
        >>> assert not print1 in pubkeys.fingerprints
        >>> result = gpg.import_keys('foo')
        >>> assert not result
        >>> result = gpg.import_keys(pubkey1)
        >>> pubkeys = gpg.list_keys()
        >>> seckeys = gpg.list_keys(secret=True)
        >>> assert not print1 in seckeys.fingerprints
        >>> assert print1 in pubkeys.fingerprints
        >>> result = gpg.import_keys(seckey1)
        >>> assert result
        >>> seckeys = gpg.list_keys(secret=True)
        >>> pubkeys = gpg.list_keys()
        >>> assert print1 in seckeys.fingerprints
        >>> assert print1 in pubkeys.fingerprints
        >>> assert print2 in pubkeys.fingerprints
        """
        ## xxx need way to validate that key_data is actually a valid GPG key
        ##     it might be possible to use --list-packets and parse the output

        result = self.result_map['import'](self)
        logger.debug('import_keys: %r', key_data[:256])
        data = _make_binary_stream(key_data, self.encoding)
        self._handle_io(['--import'], data, result, binary=True)
        logger.debug('import_keys result: %r', result.__dict__)
        data.close()
        return result

    def recv_keys(self, keyserver, *keyids):
        """Import a key from a keyserver

        >>> import shutil
        >>> shutil.rmtree("keys")
        >>> gpg = GPG(gpghome="keys")
        >>> result = gpg.recv_keys('pgp.mit.edu', '3FF0DB166A7476EA')
        >>> assert result

        """
        safe_keyserver = _fix_unsafe(keyserver)

        result = self.result_map['import'](self)
        data = _make_binary_stream("", self.encoding)
        args = ['--keyserver', keyserver, '--recv-keys']

        if keyids:
            if keyids is not None:
                safe_keyids = ' '.join(
                    [(lambda: _fix_unsafe(k))() for k in keyids])
                logger.debug('recv_keys: %r', safe_keyids)
                args.extend(safe_keyids)

        self._handle_io(args, data, result, binary=True)
        data.close()
        logger.debug('recv_keys result: %r', result.__dict__)
        return result

    def delete_keys(self, fingerprints, secret=False):
        which='key'
        if secret:
            which='secret-key'
        if _is_sequence(fingerprints):
            fingerprints = ' '.join(fingerprints)
        args = ['--batch --delete-%s "%s"' % (which, fingerprints)]
        result = self.result_map['delete'](self)
        p = self._open_subprocess(args)
        self._collect_output(p, result, stdin=p.stdin)
        return result

    def export_keys(self, keyids, secret=False):
        """export the indicated keys. 'keyid' is anything gpg accepts"""
        which=''
        if secret:
            which='-secret-key'
        if _is_sequence(keyids):
            keyids = ' '.join(['"%s"' % k for k in keyids])
        args = ["--armor --export%s %s" % (which, keyids)]
        p = self._open_subprocess(args)
        # gpg --export produces no status-fd output; stdout will be
        # empty in case of failure
        #stdout, stderr = p.communicate()
        result = self.result_map['delete'](self) # any result will do
        self._collect_output(p, result, stdin=p.stdin)
        logger.debug('export_keys result: %r', result.data)
        return result.data.decode(self.encoding, self.decode_errors)

    def list_keys(self, secret=False):
        """List the keys currently in the keyring.

        >>> import shutil
        >>> shutil.rmtree("keys")
        >>> gpg = GPG(gpghome="keys")
        >>> input = gpg.gen_key_input()
        >>> result = gpg.gen_key(input)
        >>> print1 = result.fingerprint
        >>> result = gpg.gen_key(input)
        >>> print2 = result.fingerprint
        >>> pubkeys = gpg.list_keys()
        >>> assert print1 in pubkeys.fingerprints
        >>> assert print2 in pubkeys.fingerprints

        """

        which='keys'
        if secret:
            which='secret-keys'
        args = "--list-%s --fixed-list-mode --fingerprint --with-colons" % (which,)
        args = [args]
        p = self._open_subprocess(args)

        # there might be some status thingumy here I should handle... (amk)
        # ...nope, unless you care about expired sigs or keys (stevegt)

        # Get the response information
        result = self.result_map['list'](self)
        self._collect_output(p, result, stdin=p.stdin)
        lines = result.data.decode(self.encoding,
                                   self.decode_errors).splitlines()
        valid_keywords = 'pub uid sec fpr sub'.split()
        for line in lines:
            if self.verbose:
                print(line)
            logger.debug("line: %r", line.rstrip())
            if not line:
                break
            L = line.strip().split(':')
            if not L:
                continue
            keyword = L[0]
            if keyword in valid_keywords:
                getattr(result, keyword)(L)
        return result

    def gen_key(self, input):
        """
        Generate a key; you might use gen_key_input() to create the control
        input.

        >>> gpg = GPG(gpghome="keys")
        >>> input = gpg.gen_key_input()
        >>> result = gpg.gen_key(input)
        >>> assert result
        >>> result = gpg.gen_key('foo')
        >>> assert not result

        """
        args = ["--gen-key --batch"]
        result = self.result_map['generate'](self)
        f = _make_binary_stream(input, self.encoding)
        self._handle_io(args, f, result, binary=True)
        f.close()
        return result

    def gen_key_input(self, **kwargs):
        """
        Generate --gen-key input per gpg doc/DETAILS
        """
        parms = {}
        for key, val in list(kwargs.items()):
            key = key.replace('_','-').title()
            if str(val).strip():    # skip empty strings
                parms[key] = val
        parms.setdefault('Key-Type', 'RSA')
        parms.setdefault('Key-Length', 2048)
        parms.setdefault('Name-Real', "Autogenerated Key")
        parms.setdefault('Name-Comment', "Generated by gnupg.py")
        try:
            logname = os.environ['LOGNAME']
        except KeyError:
            logname = os.environ['USERNAME']
        hostname = socket.gethostname()
        parms.setdefault('Name-Email', "%s@%s"
                         % (logname.replace(' ', '_'), hostname))
        out = "Key-Type: %s\n" % parms.pop('Key-Type')
        for key, val in list(parms.items()):
            out += "%s: %s\n" % (key, val)
        out += "%commit\n"
        return out

        # Key-Type: RSA
        # Key-Length: 1024
        # Name-Real: ISdlink Server on %s
        # Name-Comment: Created by %s
        # Name-Email: isdlink@%s
        # Expire-Date: 0
        # %commit
        #
        #
        # Key-Type: DSA
        # Key-Length: 1024
        # Subkey-Type: ELG-E
        # Subkey-Length: 1024
        # Name-Real: Joe Tester
        # Name-Comment: with stupid passphrase
        # Name-Email: joe@foo.bar
        # Expire-Date: 0
        # Passphrase: abc
        # %pubring foo.pub
        # %secring foo.sec
        # %commit

    #
    # ENCRYPTION
    #
    def encrypt_file(self, file, recipients, sign=None,
            always_trust=False, passphrase=None,
            armor=True, output=None, symmetric=False):
        """Encrypt the message read from the file-like object 'file'"""
        args = ['--encrypt']
        if symmetric:
            args = ['--symmetric']
        else:
            args = ['--encrypt']
            if not _is_sequence(recipients):
                recipients = (recipients,)
            for recipient in recipients:
                args.append('--recipient "%s"' % recipient)
        if armor:   # create ascii-armored output - set to False for binary output
            args.append('--armor')
        if output:  # write the output to a file with the specified name
            if os.path.exists(output):
                os.remove(output) # to avoid overwrite confirmation message
            args.append('--output "%s"' % output)
        if sign:
            args.append('--sign --default-key "%s"' % sign)
        if always_trust:
            args.append("--always-trust")
        result = self.result_map['crypt'](self)
        self._handle_io(args, file, result, passphrase=passphrase, binary=True)
        logger.debug('encrypt result: %r', result.data)
        return result

    def encrypt(self, data, recipients, **kwargs):
        """Encrypt the message contained in the string 'data'

        >>> import shutil
        >>> if os.path.exists("keys"):
        ...     shutil.rmtree("keys")
        >>> gpg = GPG(gpghome="keys")
        >>> input = gpg.gen_key_input(passphrase='foo')
        >>> result = gpg.gen_key(input)
        >>> print1 = result.fingerprint
        >>> input = gpg.gen_key_input()
        >>> result = gpg.gen_key(input)
        >>> print2 = result.fingerprint
        >>> result = gpg.encrypt("hello",print2)
        >>> message = str(result)
        >>> assert message != 'hello'
        >>> result = gpg.decrypt(message)
        >>> assert result
        >>> str(result)
        'hello'
        >>> result = gpg.encrypt("hello again",print1)
        >>> message = str(result)
        >>> result = gpg.decrypt(message,passphrase='bar')
        >>> result.status in ('decryption failed', 'bad passphrase')
        True
        >>> assert not result
        >>> result = gpg.decrypt(message,passphrase='foo')
        >>> result.status == 'decryption ok'
        True
        >>> str(result)
        'hello again'
        >>> result = gpg.encrypt("signed hello",print2,sign=print1,passphrase='foo')
        >>> result.status == 'encryption ok'
        True
        >>> message = str(result)
        >>> result = gpg.decrypt(message)
        >>> result.status == 'decryption ok'
        True
        >>> assert result.fingerprint == print1

        """
        data = _make_binary_stream(data, self.encoding)
        result = self.encrypt_file(data, recipients, **kwargs)
        data.close()
        return result

    def decrypt(self, message, **kwargs):
        data = _make_binary_stream(message, self.encoding)
        result = self.decrypt_file(data, **kwargs)
        data.close()
        return result

    def decrypt_file(self, file, always_trust=False, passphrase=None,
                     output=None):
        args = ["--decrypt"]
        if output:  # write the output to a file with the specified name
            if os.path.exists(output):
                os.remove(output) # to avoid overwrite confirmation message
            args.append('--output "%s"' % output)
        if always_trust:
            args.append("--always-trust")
        result = self.result_map['crypt'](self)
        self._handle_io(args, file, result, passphrase, binary=True)
        logger.debug('decrypt result: %r', result.data)
        return result
