#!/usr/bin/env python

# Copyright 2018, New York University and the TUF contributors
# SPDX-License-Identifier: MIT OR Apache-2.0

"""
<Program Name>
  repo.py

<Author>
  Vladimir Diaz <vladimir.v.diaz@gmail.com>

<Started>
  January 2018.

<Copyright>
  See LICENSE-MIT OR LICENSE for licensing information.

<Purpose>
  Provide a command-line interface to create and modify TUF repositories.  The
  CLI removes the need to write Python code when creating or modifying
  repositories, which is the case with repository_tool.py and
  developer_tool.py.

<Usage>
  Note: arguments within brackets are optional.

  $ repo.py --init
      [--consistent_snapshot, --bare, --path, --root_pw, --targets_pw,
      --snapshot_pw, --timestamp_pw]
  $ repo.py --add <target> <dir> ... [--path, --recursive]
  $ repo.py --remove <glob pattern>
  $ repo.py --trust --pubkeys </path/to/pubkey> [--role]
  $ repo.py --sign </path/to/key> [--role <targets>]
  $ repo.py --key <keytype>
      [--filename <filename>
      --path </path/to/repo>, --pw [my_password]]

  $ repo.py --delegate <glob pattern> --delegatee <rolename>
      --pubkeys </path/to/pubkey>
      [role <rolename> --terminating --threshold <X>
      --sign </path/to/role_privkey>]

  $ repo.py --revoke --delegatee <rolename>
      [--role <rolename> --sign </path/to/role_privkey>]

  $ repo.py --verbose
  $ repo.py --clean [--path]
"""

# Help with Python 3 compatibility, where the print statement is a function, an
# implicit relative import is invalid, and the '/' operator performs true
# division.  Example:  print 'hello world' raises a 'SyntaxError' exception.
from __future__ import print_function
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

import os
import sys
import logging
import argparse
import shutil
import errno
import getpass
import time
import fnmatch

import tuf
import tuf.log
import tuf.formats
import tuf.repository_tool as repo_tool

import securesystemslib
from colorama import Fore
import six


# See 'log.py' to learn how logging is handled in TUF.
logger = logging.getLogger('tuf.scripts.repo')

repo_tool.disable_console_log_messages()

PROG_NAME = 'repo.py'

REPO_DIR = 'tufrepo'
CLIENT_DIR = 'tufclient'
KEYSTORE_DIR = 'tufkeystore'

ROOT_KEY_NAME = 'root_key'
TARGETS_KEY_NAME = 'targets_key'
SNAPSHOT_KEY_NAME = 'snapshot_key'
TIMESTAMP_KEY_NAME = 'timestamp_key'

STAGED_METADATA_DIR = 'metadata.staged'
METADATA_DIR = 'metadata'

SUPPORTED_KEY_TYPES = ['ed25519', 'ecdsa-sha2-nistp256', 'rsa']

def process_arguments(parsed_arguments):
  """
  <Purpose>
    Create or modify the repository.  Which operation is executed depends
    on 'parsed_arguments'.

  <Arguments>
    parsed_arguments:
      The parsed arguments returned by argparse.parse_args().

  <Exceptions>
    securesystemslib.exceptions.Error, if any of the arguments are
    improperly formatted or if any of the argument could not be processed.

  <Side Effects>
    None.

  <Returns>
    None.
  """

  # Do we have a valid argparse Namespace?
  if not isinstance(parsed_arguments, argparse.Namespace):
    raise tuf.exception.Error('Invalid namespace.')

  else:
    logger.debug('We have a valid argparse Namespace: ' + repr(parsed_arguments))

  # TODO: Process all of the supported command-line actions.  --init, --clean,
  # --add, --sign, --key are currently implemented.
  if parsed_arguments.init:
    init_repo(parsed_arguments)

  if parsed_arguments.clean:
    clean_repo(parsed_arguments)

  if parsed_arguments.add:
    add_targets(parsed_arguments)

  if parsed_arguments.remove:
    remove_targets(parsed_arguments)

  if parsed_arguments.trust:
    add_verification_key(parsed_arguments)

  if parsed_arguments.sign:
    sign_role(parsed_arguments)

  if parsed_arguments.key:
    gen_key(parsed_arguments)

  if parsed_arguments.delegate:
    delegate(parsed_arguments)

  if parsed_arguments.revoke:
    revoke(parsed_arguments)



def delegate(parsed_arguments):

  if not parsed_arguments.delegatee:
    raise tuf.exceptions.Error(
        '--delegatee must be set to perform the delegation.')

  if parsed_arguments.delegatee in ['root', 'snapshot', 'timestamp', 'targets']:
    raise tuf.exceptions.Error(
        'Cannot delegate to the top-level role: ' + repr(parsed_arguments.delegatee))

  if not parsed_arguments.pubkeys:
    raise tuf.exceptions.Error(
        '--pubkeys must be set to perform the delegation.')

  public_keys = []
  for public_key in parsed_arguments.pubkeys:
    imported_pubkey = import_publickey_from_file(
        public_key)
    public_keys.append(imported_pubkey)

  repository = repo_tool.load_repository(
      os.path.join(parsed_arguments.path, REPO_DIR))

  if parsed_arguments.role == 'targets':
    repository.targets.delegate(parsed_arguments.delegatee, public_keys,
        parsed_arguments.delegate, parsed_arguments.threshold,
        parsed_arguments.terminating, list_of_targets=None,
        path_hash_prefixes=None)

    targets_private = import_privatekey_from_file(
        os.path.join(parsed_arguments.path, KEYSTORE_DIR, TARGETS_KEY_NAME),
        parsed_arguments.targets_pw)

    repository.targets.load_signing_key(targets_private)


  # A non-top-level role.
  else:
    repository.targets(parsed_arguments.role).delegate(
        parsed_arguments.delegatee, public_keys,
        parsed_arguments.delegate, parsed_arguments.threshold,
        parsed_arguments.terminating, list_of_targets=None,
        path_hash_prefixes=None)

    role_privatekey = import_privatekey_from_file(parsed_arguments.sign)

    repository.targets(parsed_arguments.role).load_signing_key(role_privatekey)


  # Update the required top-level roles, Snapshot and Timestamp, to make a new
  # release.
  snapshot_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR, SNAPSHOT_KEY_NAME),
      parsed_arguments.snapshot_pw)
  timestamp_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TIMESTAMP_KEY_NAME), parsed_arguments.timestamp_pw)

  repository.snapshot.load_signing_key(snapshot_private)
  repository.timestamp.load_signing_key(timestamp_private)

  repository.writeall()

  # Move staged metadata directory to "live" metadata directory.
  write_to_live_repo()



def revoke(parsed_arguments):

  repository = repo_tool.load_repository(
      os.path.join(parsed_arguments.path, REPO_DIR))

  if parsed_arguments.role == 'targets':
    repository.targets.revoke(parsed_arguments.delegatee)

    targets_private = import_privatekey_from_file(
        os.path.join(parsed_arguments.path, KEYSTORE_DIR, TARGETS_KEY_NAME),
        parsed_arguments.targets_pw)

    repository.targets.load_signing_key(targets_private)


  # A non-top-level role.
  else:
    repository.targets(parsed_arguments.role).revoke(parsed_arguments.delegatee)

    role_privatekey = import_privatekey_from_file(parsed_arguments.sign)

    repository.targets(parsed_arguments.role).load_signing_key(role_privatekey)

  # Update the required top-level roles, Snapshot and Timestamp, to make a new
  # release.
  snapshot_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR, SNAPSHOT_KEY_NAME),
      parsed_arguments.snapshot_pw)
  timestamp_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TIMESTAMP_KEY_NAME), parsed_arguments.timestamp_pw)

  repository.snapshot.load_signing_key(snapshot_private)
  repository.timestamp.load_signing_key(timestamp_private)

  repository.writeall()

  # Move staged metadata directory to "live" metadata directory.
  write_to_live_repo()



def gen_key(parsed_arguments):

  if parsed_arguments.filename:
    parsed_arguments.filename = os.path.join(parsed_arguments.path,
        KEYSTORE_DIR, parsed_arguments.filename)

  keypath = None

  if parsed_arguments.key == 'ecdsa':
    keypath = securesystemslib.interface.generate_and_write_ecdsa_keypair(
      parsed_arguments.filename, password=parsed_arguments.pw)

  elif parsed_arguments.key == 'ed25519':
    keypath = securesystemslib.interface.generate_and_write_ed25519_keypair(
        parsed_arguments.filename, password=parsed_arguments.pw)

  elif parsed_arguments.key == 'rsa':
    keypath = securesystemslib.interface.generate_and_write_rsa_keypair(
        parsed_arguments.filename, password=parsed_arguments.pw)

  else:
    tuf.exceptions.Error(
        'Invalid key type: ' + repr(parsed_arguments.key) + '.  Supported'
        ' key types: "ecdsa", "ed25519", "rsa."')


  # If a filename is not given, the generated keypair is saved to the current
  # working directory.  By default, the filenames are written to <KEYID>.pub
  # and <KEYID> (private key).  Move them from the CWD to the repo's keystore.
  if not parsed_arguments.filename:
    shutil.move(keypath, os.path.join(parsed_arguments.path,
        KEYSTORE_DIR, os.path.basename(keypath)))
    shutil.move(keypath + '.pub', os.path.join(parsed_arguments.path,
        KEYSTORE_DIR, os.path.basename(keypath + '.pub')))



def import_privatekey_from_file(keypath, password=None):
  # Note: should securesystemslib support this functionality (import any
  # privatekey type)?
  # If the caller does not provide a password argument, prompt for one.
  # Password confirmation is disabled here, which should ideally happen only
  # when creating encrypted key files.
  if password is None: # pragma: no cover

    # It is safe to specify the full path of 'filepath' in the prompt and not
    # worry about leaking sensitive information about the key's location.
    # However, care should be taken when including the full path in exceptions
    # and log files.
    password = securesystemslib.interface.get_password('Enter a password for'
        ' the encrypted key (' + Fore.RED + keypath + Fore.RESET + '): ',
        confirm=False)

  # Does 'password' have the correct format?
  securesystemslib.formats.PASSWORD_SCHEMA.check_match(password)

  # Store the encrypted contents of 'filepath' prior to calling the decryption
  # routine.
  encrypted_key = None

  with open(keypath, 'rb') as file_object:
    encrypted_key = file_object.read()

  # Decrypt the loaded key file, calling the 'cryptography' library to generate
  # the derived encryption key from 'password'.  Raise
  # 'securesystemslib.exceptions.CryptoError' if the decryption fails.
  try:

    key_object = securesystemslib.keys.decrypt_key(encrypted_key.decode('utf-8'),
        password)

  except securesystemslib.exceptions.CryptoError:
    try:
      logger.debug(
          'Decryption failsed.  Attempting to import a private PEM instead.')
      key_object = securesystemslib.keys.import_rsakey_from_private_pem(
          encrypted_key, 'rsassa-pss-sha256', password)

    except securesystemslib.exceptions.CryptoError as e:
      raise tuf.exceptions.Error(repr(keypath) + ' cannot be imported, possibly'
          ' because the decryption password is incorrect.  Encryption'
          ' passwords can be specified via the --root_pw, --targets_pw,'
          ' --snapshot_pw, and --timestamp_pw command-line options.')

  if key_object['keytype'] not in SUPPORTED_KEY_TYPES:
    raise tuf.exceptions.Error('Trying to import an unsupported key'
        ' type: ' + repr(key_object['keytype'] + '.'
        '  Supported key types: ' + repr(SUPPORTED_KEY_TYPES)))

  else:
    # Add "keyid_hash_algorithms" so that equal keys with different keyids can
    # be associated using supported keyid_hash_algorithms.
    key_object['keyid_hash_algorithms'] = securesystemslib.settings.HASH_ALGORITHMS

    return key_object



def import_publickey_from_file(keypath):

  key_metadata = securesystemslib.util.load_json_file(keypath)
  key_object, junk = securesystemslib.keys.format_metadata_to_key(key_metadata)

  if key_object['keytype'] not in SUPPORTED_KEY_TYPES:
    raise tuf.exceptions.Error('Trying to import an unsupported key'
        ' type: ' + repr(key_object['keytype'] + '.'
        '  Supported key types: ' + repr(SUPPORTED_KEY_TYPES)))

  else:
    return key_object



def add_verification_key(parsed_arguments):
  if not parsed_arguments.pubkeys:
    raise tuf.exception.Error('--pubkeys must be given with --trust.')

  repository = repo_tool.load_repository(
      os.path.join(parsed_arguments.path, REPO_DIR))

  for keypath in parsed_arguments.pubkeys:
    imported_pubkey = import_publickey_from_file(keypath)

    if parsed_arguments.role == 'root':
      repository.root.add_verification_key(imported_pubkey)

    elif parsed_arguments.role == 'targets':
      repository.targets.add_verification_key(imported_pubkey)

    elif parsed_arguments.role == 'snapshot':
      repository.snapshot.add_verification_key(imported_pubkey)

    elif parsed_arguments.role == 'timestamp':
      repository.timestamp.add_verification_key(imported_pubkey)

    else:
      raise tuf.exception.Error('The given --role is not a top-level role.')

  repository.write('root', increment_version_number=False)

  # Move staged metadata directory to "live" metadata directory.
  write_to_live_repo()





def sign_role(parsed_arguments):

  repository = repo_tool.load_repository(
      os.path.join(parsed_arguments.path, REPO_DIR))

  # Was a private key path given with --sign?  If so, load the specified
  # private key. Otherwise, load the default Targets key.
  if parsed_arguments.sign != '.':
    role_privatekey = import_privatekey_from_file(parsed_arguments.sign)

  else:
    role_privatekey = import_privatekey_from_file(
        os.path.join(parsed_arguments.path, KEYSTORE_DIR, TARGETS_KEY_NAME),
        parsed_arguments.targets_pw)

  if parsed_arguments.role == 'targets':
    repository.targets.load_signing_key(role_privatekey)

  elif parsed_arguments.role in ['snapshot', 'timestamp']:
    pass

  else:
    # TODO: repository_tool.py will be refactored to clean up the following
    # approach, which adds and signs for a non-existent role.
    if not tuf.roledb.role_exists(parsed_arguments.role):

      # Load the private key keydb and set the roleinfo in roledb so that
      # metadata can be written with repository.write().
      tuf.keydb.remove_key(role_privatekey['keyid'],
          repository_name = repository._repository_name)
      tuf.keydb.add_key(
          role_privatekey, repository_name = repository._repository_name)

      expiration = tuf.formats.unix_timestamp_to_datetime(
          int(time.time() + 7889230))
      expiration = expiration.isoformat() + 'Z'

      roleinfo = {'name': parsed_arguments.role, 'keyids': [role_privatekey['keyid']],
          'signing_keyids': [role_privatekey['keyid']], 'partial_loaded': False, 'paths': {},
          'signatures': [], 'version': 1, 'expires': expiration,
          'delegations': {'keys': {}, 'roles': []}}

      tuf.roledb.add_role(parsed_arguments.role, roleinfo,
          repository_name=repository._repository_name)
      repository.write(parsed_arguments.role, increment_version_number=False)

    else:
      repository.targets(parsed_arguments.role).load_signing_key(role_privatekey)
      repository.write(parsed_arguments.role, increment_version_number=False)

  # Update the required top-level roles, Snapshot and Timestamp, to make a new
  # release.
  snapshot_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR, SNAPSHOT_KEY_NAME),
      parsed_arguments.snapshot_pw)
  timestamp_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TIMESTAMP_KEY_NAME), parsed_arguments.timestamp_pw)

  repository.snapshot.load_signing_key(snapshot_private)
  repository.timestamp.load_signing_key(timestamp_private)

  repository.writeall()

  # Move staged metadata directory to "live" metadata directory.
  write_to_live_repo()



def clean_repo(parsed_arguments):
  repo_dir = os.path.join(parsed_arguments.path, REPO_DIR)
  client_dir = os.path.join(parsed_arguments.path, CLIENT_DIR)
  keystore_dir = os.path.join(parsed_arguments.path, KEYSTORE_DIR)

  shutil.rmtree(repo_dir, ignore_errors=True)
  shutil.rmtree(client_dir, ignore_errors=True)
  shutil.rmtree(keystore_dir, ignore_errors=True)



def write_to_live_repo():
  staged_meta_directory = os.path.join(
      parsed_arguments.path, REPO_DIR, STAGED_METADATA_DIR)
  live_meta_directory = os.path.join(
      parsed_arguments.path, REPO_DIR, METADATA_DIR)

  shutil.rmtree(live_meta_directory, ignore_errors=True)
  shutil.copytree(staged_meta_directory, live_meta_directory)



def add_target_to_repo(target_path, repo_targets_path, repository, custom=None):
  """
  (1) Copy 'target_path' to 'repo_targets_path'.
  (2) Add 'target_path' to Targets metadata of 'repository'.
  """

  if custom is None:
    custom = {}

  if not os.path.exists(target_path):
    logger.debug(repr(target_path) + ' does not exist.  Skipping.')

  else:
    securesystemslib.util.ensure_parent_dir(
        os.path.join(repo_targets_path, target_path))
    shutil.copy(target_path, os.path.join(repo_targets_path, target_path))


    roleinfo = tuf.roledb.get_roleinfo(
        parsed_arguments.role, repository_name=repository._repository_name)

    # It is assumed we have a delegated role, and that the caller has made
    # sure to reject top-level roles specified with --role.
    if target_path not in roleinfo['paths']:
      logger.debug('Adding new target: ' + repr(target_path))
      roleinfo['paths'].update({target_path: custom})

    else:
      logger.debug('Replacing target: ' + repr(target_path))
      roleinfo['paths'].update({target_path: custom})

    tuf.roledb.update_roleinfo(parsed_arguments.role, roleinfo,
        mark_role_as_dirty=True, repository_name=repository._repository_name)




def remove_target_files_from_metadata(repository):

  if parsed_arguments.role in ['root', 'snapshot', 'timestamp']:
    raise tuf.exceptions.Error(
        'Invalid rolename specified: ' + repr(parsed_arguments.role) + '.'
        '  It must be "targets" or a delegated rolename.')

  else:
    # NOTE: The following approach of using tuf.roledb to update the target
    # files will be modified in the future when the repository tool's API is
    # refactored.
    roleinfo = tuf.roledb.get_roleinfo(
        parsed_arguments.role, repository._repository_name)

    for glob_pattern in parsed_arguments.remove:
      for path in list(six.iterkeys(roleinfo['paths'])):
        if fnmatch.fnmatch(path, glob_pattern):
          del roleinfo['paths'][path]

        else:
          logger.debug('Delegated path ' + repr(path) + ' does not match'
              ' given path/glob pattern ' +  repr(glob_pattern))
          continue

    tuf.roledb.update_roleinfo(
        parsed_arguments.role, roleinfo, mark_role_as_dirty=True,
        repository_name=repository._repository_name)



def add_targets(parsed_arguments):
  target_paths = os.path.join(parsed_arguments.add)
  repo_targets_path = os.path.join(parsed_arguments.path, REPO_DIR, 'targets')
  repository = repo_tool.load_repository(
      os.path.join(parsed_arguments.path, REPO_DIR))

  # Copy the target files in --path to the repo directory, and
  # add them to Targets metadata.  Make sure to also copy & add files
  # in directories (and subdirectories, if --recursive is True).
  for target_path in target_paths:
    if os.path.isdir(target_path):
      for sub_target_path in repository.get_filepaths_in_directory(
          target_path, parsed_arguments.recursive):
        add_target_to_repo(sub_target_path, repo_targets_path, repository)

    else:
      add_target_to_repo(target_path, repo_targets_path, repository)

  # Examples of how the --pw command-line option is interpreted:
  # repo.py --init': parsed_arguments.pw = 'pw'
  # repo.py --init --pw my_password: parsed_arguments.pw = 'my_password'
  # repo.py --init --pw: The user is prompted for a password, as follows:
  if not parsed_arguments.pw:
    parsed_arguments.pw = securesystemslib.interface.get_password(
        prompt='Enter a password for the top-level role keys: ', confirm=True)

  if parsed_arguments.role == 'targets':
    # Load the top-level, non-root, keys to make a new release.
    targets_private = import_privatekey_from_file(
        os.path.join(parsed_arguments.path, KEYSTORE_DIR, TARGETS_KEY_NAME),
        parsed_arguments.pw)
    repository.targets.load_signing_key(targets_private)
    repository.write('targets', increment_version_number=True)

  elif parsed_arguments.role not in ['root', 'snapshot', 'timestamp']:
    repository.write(parsed_arguments.role, increment_version_number=True)

  snapshot_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR, SNAPSHOT_KEY_NAME),
      parsed_arguments.snapshot_pw)
  timestamp_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TIMESTAMP_KEY_NAME), parsed_arguments.timestamp_pw)

  repository.snapshot.load_signing_key(snapshot_private)
  repository.timestamp.load_signing_key(timestamp_private)

  repository.writeall()

  # Move staged metadata directory to "live" metadata directory.
  write_to_live_repo()



def remove_targets(parsed_arguments):
  target_paths = os.path.join(parsed_arguments.remove)

  repo_targets_path = os.path.join(parsed_arguments.path, REPO_DIR, 'targets')
  repository = repo_tool.load_repository(
      os.path.join(parsed_arguments.path, REPO_DIR))

  # Remove target files from the Targets metadata (or the role specified in
  # --role) that match the glob patterns specified in --remove.
  remove_target_files_from_metadata(repository)

  # Examples of how the --pw command-line option is interpreted:
  # repo.py --init': parsed_arguments.pw = 'pw'
  # repo.py --init --pw my_password: parsed_arguments.pw = 'my_password'
  # repo.py --init --pw: The user is prompted for a password, as follows:
  if not parsed_arguments.pw:
    parsed_arguments.pw = securesystemslib.interface.get_password(
        prompt='Enter a password for the top-level role keys: ', confirm=True)

  # Load the top-level, non-root, keys to make a new release.
  targets_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR, TARGETS_KEY_NAME),
      parsed_arguments.targets_pw)
  snapshot_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR, SNAPSHOT_KEY_NAME),
      parsed_arguments.snapshot_pw)
  timestamp_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TIMESTAMP_KEY_NAME), parsed_arguments.timestamp_pw)

  repository.targets.load_signing_key(targets_private)
  repository.snapshot.load_signing_key(snapshot_private)
  repository.timestamp.load_signing_key(timestamp_private)

  repository.writeall()

  # Move staged metadata directory to "live" metadata directory.
  write_to_live_repo()



def init_repo(parsed_arguments):
  """
  Create a repo at the specified location in --path (the current working
  directory, by default).  Each top-level role has one key, if --bare' is False
  (default).
  """

  repo_path = os.path.join(parsed_arguments.path, REPO_DIR)
  repository = repo_tool.create_new_repository(repo_path)

  if not parsed_arguments.bare:
    set_top_level_keys(repository)
    repository.writeall(
        consistent_snapshot=parsed_arguments.consistent_snapshot)

  else:
    repository.write(
        'root', consistent_snapshot=parsed_arguments.consistent_snapshot)
    repository.write('targets')
    repository.write('snapshot')
    repository.write('timestamp')

  write_to_live_repo()

  # Create the client files.  The client directory contains the required
  # directory structure and metadata files for clients to successfully perform
  # an update.
  repo_tool.create_tuf_client_directory(
      os.path.join(parsed_arguments.path, REPO_DIR),
      os.path.join(parsed_arguments.path, CLIENT_DIR, REPO_DIR))



def set_top_level_keys(repository):
  """
  Generate, write, and set the top-level keys.  'repository' is modified.
  """

  # Examples of how the --pw command-line option is interpreted:
  # repo.py --init': parsed_arguments.pw = 'pw'
  # repo.py --init --pw my_pw: parsed_arguments.pw = 'my_pw'
  # repo.py --init --pw: The user is prompted for a password, here.
  if not parsed_arguments.pw:
    parsed_arguments.pw = securesystemslib.interface.get_password(
        prompt='Enter a password for the top-level role keys: ', confirm=True)

  repo_tool.generate_and_write_ecdsa_keypair(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      ROOT_KEY_NAME), password=parsed_arguments.root_pw)
  repo_tool.generate_and_write_ecdsa_keypair(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TARGETS_KEY_NAME), password=parsed_arguments.targets_pw)
  repo_tool.generate_and_write_ecdsa_keypair(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      SNAPSHOT_KEY_NAME), password=parsed_arguments.snapshot_pw)
  repo_tool.generate_and_write_ecdsa_keypair(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TIMESTAMP_KEY_NAME), password=parsed_arguments.timestamp_pw)

  # Import the public keys.  They are needed so that metadata roles are
  # assigned verification keys, which clients need in order to verify the
  # signatures created by the corresponding private keys.
  root_public = import_publickey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      ROOT_KEY_NAME) + '.pub')
  targets_public = import_publickey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TARGETS_KEY_NAME) + '.pub')
  snapshot_public = import_publickey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      SNAPSHOT_KEY_NAME) + '.pub')
  timestamp_public = import_publickey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TIMESTAMP_KEY_NAME) + '.pub')

  # Import the private keys.  They are needed to generate the signatures
  # included in metadata.
  root_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      ROOT_KEY_NAME), parsed_arguments.root_pw)
  targets_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TARGETS_KEY_NAME), parsed_arguments.targets_pw)
  snapshot_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      SNAPSHOT_KEY_NAME), parsed_arguments.snapshot_pw)
  timestamp_private = import_privatekey_from_file(
      os.path.join(parsed_arguments.path, KEYSTORE_DIR,
      TIMESTAMP_KEY_NAME), parsed_arguments.timestamp_pw)

  # Add the verification keys to the top-level roles.
  repository.root.add_verification_key(root_public)
  repository.targets.add_verification_key(targets_public)
  repository.snapshot.add_verification_key(snapshot_public)
  repository.timestamp.add_verification_key(timestamp_public)

  # Load the previously imported signing keys for the top-level roles so that
  # valid metadata can be written.
  repository.root.load_signing_key(root_private)
  repository.targets.load_signing_key(targets_private)
  repository.snapshot.load_signing_key(snapshot_private)
  repository.timestamp.load_signing_key(timestamp_private)



def parse_arguments():
  """
  <Purpose>
    Parse the command-line arguments.  Also set the logging level, as specified
    via the --verbose argument (2, by default).

    Example:
      # Create a TUF repository in the current working directory.  The
      # top-level roles are created, each containing one key.
      $ repo.py --init

      $ repo.py --init --bare --consistent-snapshot --verbose 3

    If a required argument is unset, a parser error is printed and the script
    exits.

  <Arguments>
    None.

  <Exceptions>
    None.

  <Side Effects>
    Sets the logging level for TUF logging.

  <Returns>
    A tuple ('options.REPOSITORY_PATH', command, command_arguments).  'command'
    'command_arguments' correspond to a repository tool fuction.
  """

  parser = argparse.ArgumentParser(
      description='Create or modify a TUF repository.')

  parser.add_argument('-i', '--init', action='store_true',
      help='Create a repository.  The repository is created in the current'
      ' working directory unless --path is specified.')

  parser.add_argument('-p', '--path', nargs='?', default='.',
      metavar='</path/to/repo_dir>', help='Specify a repository path.  If used'
      ' with --init, the initialized repository is saved to the given'
      ' path.')

  parser.add_argument('-b', '--bare', action='store_true',
      help='If initializing a repository, neither create nor set keys'
      ' for any of the top-level roles.  False, by default.')

  parser.add_argument('--consistent_snapshot', action='store_true',
      help='Set consistent snapshots for an initialized repository.'
      '  Consistent snapshot is False by default.')

  parser.add_argument('-c', '--clean', type=str, nargs='?', const='.',
      metavar='</path/to/repo_dir', help='Delete the repo files from the'
      ' specified directory.  If a directory is not specified, the current'
      ' working directory is cleaned.')

  parser.add_argument('-a', '--add', type=str, nargs='+',
      metavar='</path/to/file>', help='Add one or more target files to the'
      ' "targets" role (or the role specified in --role).  If a directory'
      ' is given, all files in the directory are added.')

  parser.add_argument('--remove', type=str, nargs='+',
      metavar='<glob pattern>', help='Remove one or more target files from the'
      ' "targets" role (or the role specified in --role).')

  parser.add_argument('--role', nargs='?', type=str, const='targets',
      default='targets', metavar='<rolename>', help='Specify a rolename.'
      ' The rolename "targets" is used by default.')

  parser.add_argument('-r', '--recursive', action='store_true',
      help='By setting -r, any directory specified with --add is processed'
      ' recursively.  If unset, the default behavior is to not add target'
      ' files in subdirectories.')

  parser.add_argument('-k', '--key', type=str, nargs='?', const='ecdsa',
      default=None, choices=['ecdsa', 'ed25519', 'rsa'],
      help='Generate an ECDSA, Ed25519, or RSA key.  An ECDSA key is'
      ' created if the key type is unspecified.')

  parser.add_argument('--filename', nargs='?', default=None, const=None,
      metavar='<filename>', help='Specify a filename.  This option can'
      ' be used to name a generated key file.')

  parser.add_argument('--trust', action='store_true',
      help='Indicate the trusted key(s) (via --pubkeys) for the role in --role.'
      '  This action modifies Root metadata with the trusted key(s).')

  parser.add_argument('--sign', nargs='?', type=str, const='.',
      default=None, metavar='</path/to/privkey>', help='Sign the "targets"'
      ' metadata (or the one for --role) with the specified key.')

  parser.add_argument('--pw', nargs='?', default='pw', metavar='<password>',
      help='Specify a password. "pw" is used if --pw is unset, or a'
          ' password can be entered via a prompt by specifying --pw by itself.'
          '  This option can be used with --sign and --key.')

  parser.add_argument('--root_pw', nargs='?', default='pw', metavar='<password>',
      help='Specify a Root password. "pw" is used if --pw is unset, or a'
      ' password can be entered via a prompt by specifying --pw by itself.')

  parser.add_argument('--targets_pw', nargs='?', default='pw', metavar='<password>',
      help='Specify a Targets password. "pw" is used if --pw is unset, or a'
      ' password can be entered via a prompt by specifying --pw by itself.')

  parser.add_argument('--snapshot_pw', nargs='?', default='pw', metavar='<password>',
      help='Specify a Snapshot password. "pw" is used if --pw is unset, or a'
      ' password can be entered via a prompt by specifying --pw by itself.')

  parser.add_argument('--timestamp_pw', nargs='?', default='pw', metavar='<password>',
      help='Specify a Timestamp password. "pw" is used if --pw is unset, or a'
      ' password can be entered via a prompt by specifying --pw by itself.')

  parser.add_argument('-d', '--delegate', type=str, nargs='+',
      metavar='<glob pattern>', help='Delegate trust of target files'
      ' from the "targets" role (or --role) to some other role (--delegatee).'
      '  The named delegatee is trusted to sign for the target files that'
      ' match the glob pattern(s).')

  parser.add_argument('--delegatee', nargs='?', type=str, const=None,
      default=None, metavar='<rolename>', help='Specify the rolename'
      ' of the delegated role.  Can be used with --delegate.')

  parser.add_argument('-t', '--terminating', action='store_true',
      help='Set the terminating flag to True.  Can be used with --delegate.')

  parser.add_argument('--threshold', type=int, default=1, metavar='<int>',
      help='Set the threshold number of signatures'
      ' needed to validate a metadata file.  Can be used with --delegate.')

  parser.add_argument('--pubkeys', type=str, nargs='+',
      metavar='</path/to/pubkey_file>', help='Specify one or more public keys'
      ' for the delegated role.  Can be used with --delegate.')

  parser.add_argument('--revoke', action='store_true',
      help='Revoke trust of target files from a delegated role.')

  # Add the parser arguments supported by PROG_NAME.
  parser.add_argument('-v', '--verbose', type=int, default=2,
      choices=range(0, 6), help='Set the verbosity level of logging messages.'
      ' The lower the setting, the greater the verbosity.  Supported logging'
      ' levels: 0=UNSET, 1=DEBUG, 2=INFO, 3=WARNING, 4=ERROR,'
      ' 5=CRITICAL')

  # Should we include usage examples in the help output?

  parsed_args = parser.parse_args()

  # Set the logging level.
  if parsed_args.verbose == 5:
    tuf.log.set_log_level(logging.CRITICAL)

  elif parsed_args.verbose == 4:
    tuf.log.set_log_level(logging.ERROR)

  elif parsed_args.verbose == 3:
    tuf.log.set_log_level(logging.WARNING)

  elif parsed_args.verbose == 2:
    tuf.log.set_log_level(logging.INFO)

  elif parsed_args.verbose == 1:
    tuf.log.set_log_level(logging.DEBUG)

  else:
    tuf.log.set_log_level(logging.NOTSET)

  return parsed_args



if __name__ == '__main__':

  # Parse the arguments and set the logging level.
  parsed_arguments = parse_arguments()

  # Create or modify the repository depending on the option specified on the
  # command line.  For example, the following adds the 'foo.bar.gz' to the
  # default repository and updates the relevant metadata (i.e., Targets,
  # Snapshot, and Timestamp metadata are updated):
  # $ repo.py --add foo.bar.gz

  try:
    process_arguments(parsed_arguments)

  except (tuf.exceptions.Error) as e:
    sys.stderr.write('Error: ' + str(e) + '\n')
    sys.exit(1)

  # Successfully created or updated the TUF repository.
  sys.exit(0)
