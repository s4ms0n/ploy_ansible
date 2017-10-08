import argparse
import getpass
import logging
import pkg_resources
import os
import subprocess
import sys
from binascii import b2a_base64
from lazy import lazy
from ploy.common import yesno
from operator import attrgetter


log = logging.getLogger('ploy_ansible')


ansible_paths = dict(
    connection=[os.path.join(os.path.abspath(os.path.dirname(__file__)), 'connection_plugins')],
    lookup=[os.path.join(os.path.abspath(os.path.dirname(__file__)), 'lookup_plugins')])


def get_ansible_version():
    from ansible import __version__
    return tuple(int(x) for x in __version__.split('.'))


def inject_ansible_paths(ctrl=None):
    # collect and inject ansible paths (roles and library) from entrypoints
    try:
        import ansible.constants as C
    except ImportError:
        log.error("Can't import ansible, check whether it's installed correctly.")
        sys.exit(1)
    if ctrl is not None:
        from ansible.playbook.play_context import PlayContext
        from ploy_ansible.inventory import InventoryManager
        PlayContext._ploy_ctrl = ctrl
        InventoryManager._ploy_ctrl = ctrl
    if getattr(inject_ansible_paths, 'done', False):
        return
    from ansible.utils.display import Display
    global display
    display = sys.modules['__main__'].display = Display()
    if get_ansible_version() >= (2, 5):
        from ansible import __version__
        log.warn(
            "You are using an untested version %s of ansible. "
            "The latest tested version is 2.4.X. "
            "Any errors may be caused by that newer version." % __version__)
    extra_roles = []
    extra_library = []
    plugin_path_names = set(x for x in dir(C) if x.endswith('_PLUGIN_PATH'))
    extra_plugins = {}
    for entrypoint in pkg_resources.iter_entry_points(group='ansible_paths'):
        pathinfo = entrypoint.load()
        extra_roles.extend(pathinfo.get('roles', []))
        extra_library.extend(pathinfo.get('library', []))
        for key in pathinfo:
            plugin_path_name = 'DEFAULT_%s_PLUGIN_PATH' % key.upper()
            if plugin_path_name in plugin_path_names:
                extra_plugins.setdefault(plugin_path_name, []).extend(pathinfo[key])
    C.DEFAULT_ROLES_PATH[0:0] = extra_roles
    C.DEFAULT_MODULE_PATH[0:0] = extra_library
    C.DEFAULT_TRANSPORT = 'execnet_connection'
    for attr in extra_plugins:
        getattr(C, attr)[0:0] = extra_plugins[attr]
    import ansible.inventory.manager
    from ploy_ansible.inventory import InventoryManager
    ansible.inventory.manager.InventoryManager = InventoryManager
    inject_ansible_paths.done = True


def get_playbooks_directory(main_config):
    ansible_config = main_config.get('global', {}).get('ansible', {})
    default = os.path.dirname(main_config.path)
    return ansible_config.get('playbooks-directory', default)


class NullSource:
    def get(self, fail_on_error=True):
        return

    def set(self, key):
        log.error("No vault password source set.")
        sys.exit(1)

    def delete(self):
        log.error("No vault password source set.")
        sys.exit(1)


class KeyringSource:
    keyring = None

    def __init__(self, id):
        if self.keyring is None:
            try:
                import keyring
            except ImportError:
                log.error("Couldn't import 'keyring' library.")
                sys.exit(1)
            self.keyring = keyring
        if not id:
            log.error("No unique id for vault password in keyring secified.")
            sys.exit(1)
        self.id = id

    def get(self, fail_on_error=True):
        result = self.keyring.get_password("ploy_ansible", self.id)
        if result is None and fail_on_error:
            log.error("No password stored in keyring for service 'ploy_ansible' with username '%s'." % self.id)
            log.info("Use the 'vault-key' command to manage your keys.")
            sys.exit(1)
        return result

    def set(self, key):
        if not key:
            log.error("You can't use an empty key.")
            sys.exit(1)
        self.keyring.set_password("ploy_ansible", self.id, key)

    def delete(self):
        try:
            self.keyring.delete_password("ploy_ansible", self.id)
        except self.keyring.errors.PasswordDeleteError as e:
            log.error("PasswordDeleteError: %s" % e)
            sys.exit(1)
        log.info("Key deleted from keyring for service 'ploy_ansible' with username '%s'." % self.id)


def get_vault_password_source(main_config, option='vault-password-source'):
    ansible_config = main_config.get('global', {}).get('ansible', {})
    src = ansible_config.get(option)
    if src is None:
        return NullSource()
    return KeyringSource(src)


def run_cli(ctrl, name, sub, argv, myclass=None):
    inject_ansible_paths(ctrl)
    import ansible.constants as C
    from ansible import errors
    from ansible.module_utils._text import to_text
    import shutil
    try:
        from ansible.parsing.vault import VaultLib
    except ImportError:
        VaultLib = None
    if myclass is None:
        myclass = "%sCLI" % sub.capitalize()
    mycli = getattr(
        __import__("ansible.cli.%s" % sub, fromlist=[myclass]),
        myclass)
    try:
        cli = mycli(["%s %s" % (ctrl.progname, name)] + argv)
        cli.parse()
        exit_code = cli.run()
    except errors.AnsibleOptionsError as e:
        cli.parser.print_help()
        display.error(to_text(e), wrap_text=False)
        exit_code = 5
    except errors.AnsibleParserError as e:
        display.error(to_text(e), wrap_text=False)
        exit_code = 4
    except errors.AnsibleError as e:
        display.error(to_text(e), wrap_text=False)
        exit_code = 1
    except KeyboardInterrupt:
        display.error("User interrupted execution")
        exit_code = 99
    finally:
        # Remove ansible tempdir
        shutil.rmtree(C.DEFAULT_LOCAL_TMP, True)
    if exit_code != 0:
        sys.exit(exit_code)


class AnsibleCmd(object):
    """Run Ansible"""

    def __init__(self, ctrl):
        self.ctrl = ctrl

    def __call__(self, argv, help):
        return run_cli(self.ctrl, 'ansible', 'adhoc', argv, myclass='AdHocCLI')


class AnsiblePlaybookCmd(object):
    """Run Ansible playbook"""

    def __init__(self, ctrl):
        self.ctrl = ctrl

    def __call__(self, argv, help):
        return run_cli(self.ctrl, 'playbook', 'playbook', argv)


class AnsibleConfigureCmd(object):
    """Configure an instance (ansible playbook run) after it has been started."""

    def __init__(self, ctrl):
        self.ctrl = ctrl

    def get_completion(self):
        instances = set()
        for instance in self.ctrl.instances:
            if self.ctrl.instances[instance].has_playbook():
                instances.add(instance)
        return sorted(instances)

    def __call__(self, argv, help):
        parser = argparse.ArgumentParser(
            prog="%s configure" % self.ctrl.progname,
            description=help)
        parser.add_argument(
            '-v', '--verbose', default=False, action="count",
            help="verbose mode (-vvv for more, -vvvv to enable connection debugging)")
        parser.add_argument(
            '-t', '--tags',
            dest='only_tags',
            default='all',
            help="only run plays and tasks tagged with these values")
        parser.add_argument(
            '--skip-tags',
            dest='skip_tags',
            help="only run plays and tasks whose tags do not match these values")
        parser.add_argument(
            '-e', '--extra-vars',
            dest="extra_vars",
            action="append",
            default=[],
            help="set additional variables as key=value or YAML/JSON")
        parser.add_argument(
            "instance",
            nargs=1,
            metavar="instance",
            help="Name of the instance from the config.",
            choices=self.get_completion())
        args = parser.parse_args(argv)
        instance = self.ctrl.instances[args.instance[0]]
        only_tags = args.only_tags.split(",")
        skip_tags = args.skip_tags
        if skip_tags is not None:
            skip_tags = skip_tags.split(",")
        else:
            skip_tags = []
        inject_ansible_paths()
        display.verbosity = args.verbose
        options = AnsibleOptions()
        options.verbosity = args.verbose
        options.only_tags = only_tags
        options.skip_tags = skip_tags
        options.extra_vars = args.extra_vars
        instance.hooks.before_ansible_configure(instance)
        instance.configure(options=options)
        instance.hooks.after_ansible_configure(instance)


class AnsibleInventoryCmd(object):
    """Print the inventory of all instances."""

    def __init__(self, ctrl):
        self.ctrl = ctrl

    def __call__(self, argv, help):
        parser = argparse.ArgumentParser(
            prog="%s inventory" % self.ctrl.progname,
            description=help)
        parser.parse_args(argv)
        inventory = _get_ansible_inventorymanager(self.ctrl, self.ctrl.config)
        groups = sorted(inventory.groups, key=attrgetter('name'))
        for group in groups:
            print group.name
            hosts = sorted(group.hosts, key=attrgetter('name'))
            for host in hosts:
                print "    %s" % host.name
            print


class AnsibleVaultKeyCmd(object):
    """Manage vault keys."""

    # Additional GPG parameters. Used for testing.
    gpg_opts = ()

    def __init__(self, ctrl):
        self.ctrl = ctrl

    def get_completion(self):
        return ('delete', 'export', 'generate', 'import', 'set')

    def __call__(self, argv, help):
        parser = argparse.ArgumentParser(
            prog="%s vault-key" % self.ctrl.progname,
            description=help)
        subparsers = parser.add_subparsers(help="sub commands of vault-key command")
        parser.add_argument(
            '-o', '--old',
            action="store_true",
            help="use 'vault-password-old-source'")
        deleteparser = subparsers.add_parser(
            "delete",
            help="delete the vault key")
        deleteparser.set_defaults(func=self.cmd_delete)
        exportparser = subparsers.add_parser(
            "export",
            help="export key from gpg encrypted file")
        exportparser.add_argument("recipient")
        exportparser.add_argument("file", nargs='?')
        exportparser.set_defaults(func=self.cmd_export)
        generateparser = subparsers.add_parser(
            "generate",
            help="generate a new random 32 byte vault key and store it")
        generateparser.set_defaults(func=self.cmd_generate)
        importparser = subparsers.add_parser(
            "import",
            help="import key from gpg encrypted file")
        importparser.add_argument("file")
        importparser.set_defaults(func=self.cmd_import)
        setparser = subparsers.add_parser(
            "set",
            help="set the vault key")
        setparser.set_defaults(func=self.cmd_set)
        args = parser.parse_args(argv)
        if args.old:
            src = get_vault_password_source(self.ctrl.config, option='vault-password-old-source')
        else:
            src = get_vault_password_source(self.ctrl.config)
        args.func(args, src)

    def cmd_export(self, args, src):
        key = src.get()
        recipient = args.recipient
        if args.file:
            fn = args.file
        else:
            fn = os.path.join(self.ctrl.config.path, "%s.key.gpg" % recipient)
        cmd = ['gpg', '--quiet', '--no-tty', '--armor', '--batch', '--encrypt']
        cmd.extend(self.gpg_opts)
        cmd.extend(['--recipient', recipient, '--output', fn])
        gpg = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        gpg.communicate(key)
        if gpg.returncode != 0:
            log.error('GPG returned non-zero exit code.')
            log.info('Command used: %s', ' '.join(cmd))
            sys.exit(gpg.returncode)
        log.info("Encrypted key into '%s'." % fn)

    def cmd_generate(self, args, src):
        if src.get(fail_on_error=False) and not yesno("There is already a key stored, do you want to replace it?"):
            sys.exit(1)
        key = b2a_base64(os.urandom(32))
        key = key.strip()
        key = key.replace('+', '-')
        key = key.replace('/', '_')
        src.set(key)

    def cmd_import(self, args, src):
        if src.get(fail_on_error=False) and not yesno("There is already a key stored, do you want to replace it?"):
            return
        cmd = ['gpg', '--quiet', '--no-tty', '--decrypt']
        cmd.extend(self.gpg_opts)
        cmd.extend([args.file])
        key = subprocess.check_output(cmd)
        src.set(key)

    def cmd_set(self, args, src):
        if src.get(fail_on_error=False) and not yesno("There is already a key stored, do you want to replace it?"):
            return
        src.set(getpass.getpass("Password for '%s': " % src.id))

    def cmd_delete(self, args, src):
        if yesno("Do you really want to delete the key for '%s'?" % src.id):
            src.delete()


class AnsibleVaultCmd(object):
    """Manage vault encrypted files."""

    def __init__(self, ctrl):
        self.ctrl = ctrl

    def get_completion(self):
        return ('create', 'decrypt', 'edit', 'encrypt', 'rekey')

    def __call__(self, argv, help):
        parser = argparse.ArgumentParser(
            prog="%s vault" % self.ctrl.progname,
            description=help)
        subparsers = parser.add_subparsers(help="sub commands of vault command")
        catparser = subparsers.add_parser("cat", help="cat contents of an encrypted file")
        catparser.add_argument("file", nargs=1)
        catparser.set_defaults(func=self.cmd_cat)
        createparser = subparsers.add_parser("create", help="create an encrypted file")
        createparser.add_argument("file", nargs=1)
        createparser.set_defaults(func=self.cmd_create)
        decryptparser = subparsers.add_parser("decrypt", help="decrypt encrypted files")
        decryptparser.add_argument("file", nargs="+")
        decryptparser.set_defaults(func=self.cmd_decrypt)
        editparser = subparsers.add_parser("edit", help="edit an encrypted file")
        editparser.add_argument("file", nargs=1)
        editparser.set_defaults(func=self.cmd_edit)
        encryptparser = subparsers.add_parser("encrypt", help="encrypt unencrypted files")
        encryptparser.add_argument("file", nargs="+")
        encryptparser.set_defaults(func=self.cmd_encrypt)
        rekeyparser = subparsers.add_parser("rekey", help="rekey encrypted files")
        rekeyparser.add_argument("file", nargs="+")
        rekeyparser.set_defaults(func=self.cmd_rekey)
        args = parser.parse_args(argv)
        args.func(args)

    @lazy
    def AnsibleError(self):
        inject_ansible_paths(self.ctrl)
        from ansible.errors import AnsibleError
        return AnsibleError

    @lazy
    def ve(self):
        inject_ansible_paths(self.ctrl)
        try:
            from ansible.parsing.vault import VaultEditor
        except ImportError:
            log.error("Your ansible installation doesn't support vaults.")
            sys.exit(1)
        return VaultEditor

    @lazy
    def vl(self):
        inject_ansible_paths(self.ctrl)
        try:
            from ansible.parsing.vault import VaultLib
        except ImportError:
            log.error("Your ansible installation doesn't support vaults.")
            sys.exit(1)
        return VaultLib

    def cmd_cat(self, args):
        password = get_vault_password_source(self.ctrl.config).get()
        this_editor = self.ve(None, password, args.file[0])
        vl = self.vl(password)
        try:
            sys.stdout.write(vl.decrypt(this_editor.read_data(this_editor.filename)))
            sys.stdout.flush()
        except self.AnsibleError as e:
            log.error("%s" % e)
            sys.exit(1)

    def cmd_create(self, args):
        password = get_vault_password_source(self.ctrl.config).get()
        this_editor = self.ve(None, password, args.file[0])
        try:
            this_editor.create_file()
        except self.AnsibleError as e:
            log.error("%s" % e)

    def cmd_decrypt(self, args):
        password = get_vault_password_source(self.ctrl.config).get()
        for f in args.file:
            this_editor = self.ve(None, password, f)
            try:
                this_editor.decrypt_file()
            except self.AnsibleError as e:
                log.error("%s" % e)
            log.info("Decrypted %s" % f)

    def cmd_edit(self, args):
        password = get_vault_password_source(self.ctrl.config).get()
        this_editor = self.ve(None, password, args.file[0])
        try:
            this_editor.edit_file()
        except self.AnsibleError as e:
            log.error("%s" % e)

    def cmd_encrypt(self, args):
        password = get_vault_password_source(self.ctrl.config).get()
        for f in args.file:
            this_editor = self.ve(None, password, f)
            try:
                this_editor.encrypt_file()
            except self.AnsibleError as e:
                log.error("%s" % e)
            log.info("Encrypted %s" % f)

    def cmd_rekey(self, args):
        old_password = get_vault_password_source(self.ctrl.config, option='vault-password-old-source').get()
        if old_password is None:
            log.error("You have to specify the old vault password source with the 'vault-password-old-source' option.")
            sys.exit(1)
        password = get_vault_password_source(self.ctrl.config).get()
        for f in args.file:
            this_editor = self.ve(None, old_password, f)
            try:
                this_editor.rekey_file(password)
            except self.AnsibleError as e:
                log.error("%s" % e)
            log.info("Rekeyed %s" % f)


def has_playbook(self):
    playbooks_directory = get_playbooks_directory(self.master.main_config)
    playbook_path = os.path.join(playbooks_directory, '%s.yml' % self.uid)
    if os.path.exists(playbook_path):
        return True
    if 'playbook' in self.config:
        return True
    if 'roles' in self.config:
        return True
    return False


def get_plays(self, loader, inventory, variable_manager, *args, **kwargs):
    inject_ansible_paths(self.master.ctrl)
    from ansible.playbook import Play, Playbook
    import ansible.errors

    host = self.uid
    playbooks_directory = get_playbooks_directory(self.master.main_config)
    loader.set_basedir(playbooks_directory)
    playbook = kwargs.pop('playbook', None)
    if playbook is None:
        playbook_path = os.path.join(playbooks_directory, '%s.yml' % self.uid)
        if os.path.exists(playbook_path):
            playbook = playbook_path
        if 'playbook' in self.config:
            if playbook is not None and playbook != self.config['playbook']:
                log.warning("Instance '%s' has the 'playbook' option set, but there is also a playbook at the default location '%s', which differs from '%s'." % (self.config_id, playbook, self.config['playbook']))
            playbook = self.config['playbook']
    if playbook is not None:
        log.info("Using playbook at '%s'." % playbook)
    roles = kwargs.pop('roles', None)
    if roles is None and 'roles' in self.config:
        roles = self.config['roles']
    if roles is not None and playbook is not None:
        log.error("You can't use a playbook and the 'roles' options at the same time for instance '%s'." % self.config_id)
        sys.exit(1)
    skip_host_check = kwargs.pop('skip_host_check', False)
    try:
        if roles is None:
            pb = Playbook.load(playbook, variable_manager=variable_manager, loader=loader)
            plays = pb.get_plays()
        else:
            if isinstance(roles, basestring):
                roles = roles.split()
            data = {
                'hosts': [host],
                'roles': roles}
            plays = [Play.load(data, variable_manager=variable_manager, loader=loader)]
    except ansible.errors.AnsibleError as e:
        log.error("AnsibleError: %s" % e)
        sys.exit(1)
    for play in plays:
        if play._attributes.get('remote_user') is None:
            play._attributes['remote_user'] = self.config.get('user', 'root')
        if self.config.get('sudo'):
            play._attributes['sudo'] = self.config.get('sudo')
        if not skip_host_check:
            hosts = play._attributes.get('hosts', None)
            if isinstance(hosts, basestring):
                hosts = hosts.split(':')
            if hosts is None:
                hosts = {}
            if self.uid not in hosts:
                log.warning("The host '%s' is not in the list of hosts (%s) of '%s'.", self.uid, ','.join(hosts), playbook)
                if not yesno("Do you really want to apply '%s' to the host '%s'?" % (playbook, self.uid)):
                    sys.exit(1)
        play._attributes['hosts'] = [self.uid]
    return plays


class AnsibleOptions(object):
    def __init__(self):
        from ansible import constants as C
        self.ask_vault_pass = None
        self.become = C.DEFAULT_BECOME
        self.become_method = C.DEFAULT_BECOME_METHOD
        self.become_user = C.DEFAULT_BECOME_USER
        self.check = False
        self.connection = C.DEFAULT_TRANSPORT
        self.diff = C.DIFF_ALWAYS
        self.forks = C.DEFAULT_FORKS
        self.inventory = None
        self.module_path = None
        self.vault_ids = []
        self.vault_password_files = None
        self.verbosity = C.DEFAULT_VERBOSITY


def configure(self, *args, **kwargs):
    from ansible.parsing.dataloader import DataLoader
    from ansible.executor.task_queue_manager import TaskQueueManager
    from ansible.utils.vars import load_extra_vars
    from ansible.vars.manager import VariableManager
    if 'options' in kwargs:
        options = kwargs['options']
    else:
        options = AnsibleOptions()
    if 'loader' in kwargs:
        loader = kwargs['loader']
    else:
        loader = DataLoader()
    inventory = self.get_ansible_inventorymanager()
    variable_manager = VariableManager(loader=loader, inventory=inventory)
    variable_manager.extra_vars = load_extra_vars(loader=loader, options=options)
    tqm = TaskQueueManager(inventory=inventory, variable_manager=variable_manager, loader=loader, options=options, passwords=None)
    plays = self.get_plays(inventory=inventory, variable_manager=variable_manager, loader=loader, *args, **kwargs)
    import ansible.errors
    try:
        for play in plays:
            tqm.run(play=play)
    except ansible.errors.AnsibleError as e:
        log.error("AnsibleError: %s" % e)
        sys.exit(1)


class AnsibleVariablesDict(dict):
    def __getitem__(self, name):
        from ansible.utils.template import template
        return template(self.basedir, dict.__getitem__(self, name), self, fail_on_undefined=True)


def _get_ansible_inventorymanager(ctrl, main_config):
    inject_ansible_paths(ctrl)
    from ploy_ansible.inventory import InventoryManager
    vault_password = get_vault_password_source(main_config).get(fail_on_error=False)
    return InventoryManager()


def get_ansible_inventorymanager(self):
    return _get_ansible_inventorymanager(self.master.ctrl, self.master.main_config)


def get_ansible_variables(self):
    inventory = self.get_ansible_inventorymanager()
    basedir = get_playbooks_directory(self.master.ctrl.config)
    result = AnsibleVariablesDict(inventory.get_variables(self.uid))
    result.basedir = basedir
    return result


def get_vault_lib(self):
    try:
        from ansible.parsing.vault import VaultLib
    except ImportError:
        return None
    return VaultLib(get_vault_password_source(self.master.main_config).get())


def augment_instance(instance):
    if not hasattr(instance, 'has_playbook'):
        instance.has_playbook = has_playbook.__get__(instance, instance.__class__)
    if not hasattr(instance, 'get_plays'):
        instance.get_plays = get_plays.__get__(instance, instance.__class__)
    if not hasattr(instance, 'configure'):
        instance.configure = configure.__get__(instance, instance.__class__)
    if not hasattr(instance, 'get_ansible_inventorymanager'):
        instance.get_ansible_inventorymanager = get_ansible_inventorymanager.__get__(instance, instance.__class__)
    if not hasattr(instance, 'get_ansible_variables'):
        instance.get_ansible_variables = get_ansible_variables.__get__(instance, instance.__class__)
    if not hasattr(instance, 'get_vault_lib'):
        instance.get_vault_lib = get_vault_lib.__get__(instance, instance.__class__)


def get_commands(ctrl):
    return [
        ('ansible', AnsibleCmd(ctrl)),
        ('playbook', AnsiblePlaybookCmd(ctrl)),
        ('configure', AnsibleConfigureCmd(ctrl)),
        ('inventory', AnsibleInventoryCmd(ctrl)),
        ('vault', AnsibleVaultCmd(ctrl)),
        ('vault-key', AnsibleVaultKeyCmd(ctrl))]


def get_massagers():
    from ploy.config import PathMassager
    return [
        PathMassager('ansible', 'playbooks-directory'),
        PathMassager('global', 'playbooks-directory')]


plugin = dict(
    augment_instance=augment_instance,
    get_commands=get_commands,
    get_massagers=get_massagers)
