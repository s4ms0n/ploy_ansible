from ploy_ansible.inventory import InventoryManager


def test_inventory_yml_vars(ctrl, ployconf, tempdir):
    from ansible.parsing.dataloader import DataLoader
    from ansible.vars.manager import VariableManager
    ctrl.configfile = ployconf.path
    ployconf.fill([
        '[dummy-instance:foo]',
        'host = foo',
        'test = 1'])
    tempdir['group_vars/all.yml'].fill([
        '---',
        'ham: egg'])
    tempdir['host_vars/default-foo.yml'].fill([
        '---',
        'blubber: bla'])
    InventoryManager._ploy_ctrl = ctrl
    inventory = InventoryManager()
    loader = DataLoader()
    loader.set_basedir(tempdir.directory)
    vm = VariableManager(loader=loader, inventory=inventory)
    variables = vm.get_vars(host=inventory.get_host('default-foo'))
    assert set(variables).intersection(('blubber', 'ham', 'ploy_test')) == set(
        ('blubber', 'ham', 'ploy_test'))
    assert variables['blubber'] == 'bla'
    assert variables['ham'] == 'egg'
    assert variables['ploy_test'] == '1'


def test_inventory_groups(ctrl, ployconf, tempdir):
    from ansible.parsing.dataloader import DataLoader
    from ansible.vars.manager import VariableManager
    ctrl.configfile = ployconf.path
    ployconf.fill([
        '[dummy-instance:foo]',
        'host = foo',
        'test = 1',
        'groups = foo bar'])
    tempdir['group_vars/foo.yml'].fill([
        '---',
        'ham: egg'])
    tempdir['group_vars/bar.yml'].fill([
        '---',
        'blubber: bla'])
    InventoryManager._ploy_ctrl = ctrl
    inventory = InventoryManager()
    loader = DataLoader()
    loader.set_basedir(tempdir.directory)
    vm = VariableManager(loader=loader, inventory=inventory)
    variables = vm.get_vars(host=inventory.get_host('default-foo'))
    assert set(variables).intersection(('blubber', 'ham', 'ploy_test')) == set(
        ('blubber', 'ham', 'ploy_test'))
    assert variables['blubber'] == 'bla'
    assert variables['ham'] == 'egg'
    assert variables['ploy_test'] == '1'
