# Core Imports
import re
import os
import errno
import shutil
import inspect
import zipfile
import collections
from glob import glob
from tempfile import mkdtemp
from datetime import datetime
from importlib import import_module
from contextlib import contextmanager

# 3rd Party Imports
import click
from click import echo

import mongoengine

# Local Imports
from .models import Migration
from .mongo import drop as drop_mongo_db
from .mongo import copy_db as copy_mongo_db
from .mongo import MongoMigrationHistory, MongoBackedMigration
from .mongo import dump_db, restore as restore_mongo_db


MIGRATION_TEMPLATE = '''
from monarch import {base_class}

class {migration_class_name}({base_class}):

    def run(self):
        """Write the code here that will migrate the database from one state to the next
            No Need to handle exceptions -- we will take care of that for you
        """
        raise NotImplementedError
'''

CONFIG_TEMPLATE = """
# monarch settings file, generated by monarch init
# feal free to edit it with your application specific settings

ENVIRONMENTS = {
    'production': {
        'host': 'your-host',
        'port': 12345,
        'db_name': 'your-db-name',
        'username': 'asdf',
        'password': 'asdfdf'
    },
    'development': {
        'host': 'your-host',
        'port': 12345,
        'db_name': 'your-db-name',
        'username': 'asdf',
        'password': 'asdfdf'
    },
}


# If you want to use the backups feature uncomment and fill out the following:
# BACKUPS = {
#     'S3': {
#         'bucket_name': 'your_bucket_name',
#         'aws_access_key_id': 'aws_access_key_id',
#         'aws_secret_access_key': 'aws_secret_access_key',
#     }
# }

# OR

# BACKUPS = {
#     'LOCAL': {
#         'backup_dir': 'path_to_backups',
#     }
# }


"""

CAMEL_PAT = re.compile(r'([A-Z])')
UNDER_PAT = re.compile(r'_([a-z])')


class Config(object):

    def __init__(self):
        self.migration_directory = None
        self.config_directory = None

    def configure_from_settings_file(self):
        settings = import_module('migrations.settings')

        if not hasattr(settings, 'ENVIRONMENTS'):
            exit_with_message('Configuration file should have a ENVIRONMENTS method set')
        else:
            self.environments = settings.ENVIRONMENTS

        if hasattr(settings, 'BACKUPS'):
            self.backups = settings.BACKUPS


def establish_datastore_connection(environment):
    mongo_name = environment['db_name']
    mongo_port = int(environment['port'])
    mongoengine.connect(mongo_name, port=mongo_port)


pass_config = click.make_pass_decorator(Config, ensure=True)


@click.group()
@pass_config
@click.pass_context
def cli(ctx, config):
    """ Your friendly migration manager

        To get help on a specific function you may append --help to the function
        i.e.
        monarch generate --help
    """
    if ctx.invoked_subcommand != 'init':
        config.configure_from_settings_file()


@cli.command()
@click.argument('name')
@pass_config
def generate(config, name):
    """
    Generates a migration file.  pass it a name.  execute like so:

    monarch generate [migration_name]

    i.e.

    monarch generate add_indexes_to_user_collection

    """
    create_migration_directory_if_necessary(config.migration_directory)
    migration_name = generate_migration_name(config.migration_directory, name)
    class_name = "{}Migration".format(underscore_to_camel(name))
    output = MIGRATION_TEMPLATE.format(migration_class_name=class_name, base_class='MongoBackedMigration')
    with open(migration_name, 'w') as f:
        f.write(output)
    click.echo("Generated Migration Template: [{}]".format(migration_name))


@cli.command(name='list_migrations')
@click.argument('environment')
@pass_config
def lizt(config, environment):
    """ Lists the migrations and the status against the specified environemnt

    """
    if environment not in config.environments:
        exit_with_message("Environment not described in settings.py")

    migrations_on_file_system = find_migrations(config)

    establish_datastore_connection(config.environments[environment])

    if migrations_on_file_system:
        click.echo("Here are the migrations:")
        echo("{:50} {}".format('MIGRATIONS', 'STATUS'))
        for migration_name in migrations_on_file_system:
            migration_meta = MongoMigrationHistory.find_by_key(migration_name)
            if migration_meta:
                echo("{:50} {}".format(migration_name, migration_meta.state))
            else:
                echo("{:50} NOT RUN".format(migration_name))

    else:
        click.echo("No pending migrations")


@cli.command()
@click.argument('environment')
@pass_config
def migrate(config, environment):
    """
    Runs all migrations that have yet to have run.
    :return:
    """
    if environment not in config.environments:
        exit_with_message("Environment not described in settings.py")

    # 1) Find all migrations in the migrations/ directory
    # key = name, value = MigrationClass
    migrations = find_migrations(config)
    if migrations:
        establish_datastore_connection(config.environments[environment])
        for k, migration_class in migrations.iteritems():
            migration_instance = migration_class()

            # 3) Run the migration -- it will only run if it has not yet been run yet
            migration_instance.process()
    else:
        click.echo("No migrations exist")


@cli.command()
@click.option('--migration-directory', default='./migrations', help='path to where you want to store your migrations')
@pass_config
def init(config, migration_directory):
    """ Generates a default setting file.

        It will it in ./migrations and will create the package if it does not exist

    """

    create_migration_directory_if_necessary(config.migration_directory)
    settings_file = os.path.join(os.path.abspath(config.migration_directory), 'settings.py')

    if os.path.exists(settings_file):
        click.confirm("A settings file already exists.  Are you sure you want to overwrite it?", abort=True)

    with open(settings_file, 'w') as f:
        f.write(CONFIG_TEMPLATE)

    msg = """We just created a shinny new configuration file for you.  You can find it here:

    {}

    You are encouraged to open it up and modify it for your needs
    """.format(settings_file)

    echo(msg)


# @cli.command()
# @pass_config
# def initialize_sql_database():
#     engine = sqlalchemy.create_engine('sqlite:///:memory:', echo=False)


@cli.command()
@click.argument('from_to')
@pass_config
def copy_db(config, from_to):
    """ Copys a database and imports into another database

        Example

        monarch import_db production:local
        monarch import_db staging:local

    """
    if ':' not in from_to:
        exit_with_message("Expecting from:to syntax like production:local")

    from_db, to_db = from_to.split(':')

    if config.environments is None:
        exit_with_message('Configuration file should have a ENVIRONMENTS set')

    if from_db not in config.environments:
        exit_with_message('Environemnts does not have a specification for {}'.format(from_db))

    if to_db not in config.environments:
        exit_with_message('Environemnts does not have a specification for {}'.format(to_db))

    if click.confirm('Are you SURE you want to copy data from {} into {}?'.format(from_db, to_db)):
        echo()
        echo("Okay, you asked for it ...")
        echo()
        copy_mongo_db(config.environments[from_db], config.environments[to_db])

@cli.command()
@click.argument('environment')
@pass_config
def drop_db(config, environment):
    """ drops the database -- ARE YOU SURE YOU WANT TO DO THIS
    """
    drop_mongo_db(config.environments[environment])



def find_migrations(config):
    migrations = {}
    click.echo("fm 1 cwd: {}".format(os.getcwd()))
    for file in glob('{}/*_migration.py'.format(config.migration_directory)):
        migration_name = os.path.splitext(os.path.basename(file))[0]
        migration_module = import_module("migrations.{}".format(migration_name))
        for name, obj in inspect.getmembers(migration_module):
            if inspect.isclass(obj) and re.search('Migration$', name) and name not in ['BaseMigration',
                                                                                       'MongoBackedMigration']:
                migrations[migration_name] = obj

    # 2) Ensure that the are ordered
    ordered = collections.OrderedDict(sorted(migrations.items()))
    return ordered


def exit_with_message(message):
    echo()
    echo(message)
    echo()
    exit()


def confirm_environment(config, environment):
    if environment not in config.environments:
        exit_with_message("{} is not in settings.  Exiting ...".format(environment))

@cli.command()
@click.argument('environment')
@pass_config
def backup(config, environment):
    """ Backs up a given datastore
        It is configured in the BACKUPS section of settings
        You can back up locally or to S3
    """

    if not hasattr(config, 'backups'):
        exit_with_message('BACKUPS not configured, exiting')

    confirm_environment(config, environment)

    if 'LOCAL' in config.backups:
        backup_localy(config, environment, config.backups['LOCAL'])
    elif 'S3' in config.backups:
        backup_to_s3(config, environment, config.backups['S3'])
    else:
        exit_with_message('BACKUPS not configured, exiting')


def list_local_backups(local_config):

    _local_backups = local_backups(local_config)
    for backup in _local_backups:
        echo("{:50} {}".format(backup, sizeof_fmt(os.path.getsize(_local_backups[backup]))))


def list_s3_backups(s3_settings):
    pass


def sizeof_fmt(num):
    for x in ['bytes','KB','MB','GB','TB']:
        if num < 1024.0:
            return "%3.1f %s" % (num, x)
        num /= 1024.0


@cli.command()
@pass_config
def list_backups(config):
    """ Lists available backups
    """
    if config.backups is None:
        exit_with_message('BACKUPS not configured, exiting')

    if 'LOCAL' in config.backups:
        list_local_backups(config.backups['LOCAL'])
    elif 'S3' in config.backups:
        list_s3_backups(config.backups['S3'])
    else:
        exit_with_message('BACKUPS not configured, exiting')



def backup_to_s3(config, environment, s3_settings):
    raise NotImplementedError
    import os
    import boto
    import zipfile
    from boto.s3.key import Key
    from tempfile import mkdtemp, mkstemp
    from .mongo import dump_db
    # 1) dump db locally to temp file
    temp_dir = mkdtemp()
    dump_path = dump_db(enviornment, temp_dir)

    echo("Zipping File")

    # 2) compress file
    def zipdir(path, zip):
        for root, dirs, files in os.walk(path):
            for file in files:
                zip.write(os.path.join(root, file))

    zipf = zipfile.ZipFile('MongoDump.zip', 'w')
    zipdir(dump_path, zipf)
    zipf.close()

    echo("Zipping File")

    # 3) upload to s3
    conn = boto.connect_s3(config.s3.aws_access_key_id, config.s3.aws_secret_access_key)
    bucket = conn.get_bucket(config.s3.bucket_name)
    k = Key(bucket)
    k.key = zipf.filename
    bytes_written = k.set_contents_from_filename(zipf.filename)

    # 4) print out the name of the bucket
    echo("Wrote {} btyes to s3".format(bytes_written))


def backup_localy(config, env_name, local_settings):

    if 'backup_dir' not in local_settings:
        exit_with_message('Local Settings not configured correctly, expecting "backup_dir"')

    backup_dir = local_settings['backup_dir']

    if not os.path.isdir(backup_dir):
        exit_with_message('Directory [{}] does not exist.  Exiting ...'.format(backup_dir))

    # 1) dump db locally to temp file
    temp_dir = mkdtemp()

    echo("envs {}".format(config.environments))

    dump_path = dump_db(config.environments[env_name], temp_dir)
    echo("Zipping File")

    # 2) compress file
    def zipdir(path, zip):
        for root, dirs, files in os.walk(path):
            for file in files:
                zip.write(os.path.join(root, file), os.path.relpath(os.path.join(root, file), root))

    zipf = zipfile.ZipFile('MongoDump.zip', 'w')
    zipdir(dump_path, zipf)
    zipf.close()

    echo("Moving {} file into: {}".format(zipf.filename, backup_dir))

    unique_file_path = generate_unique_name(backup_dir, config.environments[env_name])

    shutil.move(zipf.filename, unique_file_path)


@cli.command()
@click.argument('from_to')
@pass_config
def restore(config, from_to):
    """ Restores a backup into a destination database.  Provide a dump name that you can get from

        monarch list_backups

        Example

        monarch restore adid-development__2014_06_18.dmp.zip:development

    """
    if ':' not in from_to:
        exit_with_message("Expecting from:to syntax like production:local")

    backup, to_db = from_to.split(':')

    if config.environments is None:
        exit_with_message('Configuration file should have a ENVIRONMENTS set')

    if to_db not in config.environments:
        exit_with_message('Environemnts does not have a specification for {}'.format(to_db))

    if backup not in backups(config):
        exit_with_message('Can not find backup {}, run monarch list_backups to see your options'.format(backup))

    if click.confirm('Are you SURE you want to retore backup into into {}? It will delete the database first'.format(to_db)):
        echo()
        echo("Okay, you asked for it ...")
        echo()
        restore_db(backups(config)[backup], config.environments[to_db])


@contextmanager
def temp_directory():
    temp_dir = mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)

def restore_db(zip_path, to_environment):
    """unzips the file then runs a restore"""
    zip = zipfile.ZipFile(zip_path)
    with temp_directory() as temp_dir:
        zip.extractall(path=temp_dir)
        restore_mongo_db(temp_dir, to_environment)

    echo()
    echo("Rock and roll that seemed to go well -- Nice work")
    echo()







def backups(config):
    """returns a dictionary of {backup_name: backup_path}"""
    if config.backups is None:
        exit_with_message('BACKUPS not configured, exiting')

    if 'LOCAL' in config.backups:
        return local_backups(config.backups['LOCAL'])
    elif 'S3' in config.backups:
        return s3_backups(config.backups['S3'])
    else:
        exit_with_message('BACKUPS not configured, exiting')

def local_backups(local_config):
    if 'backup_dir' not in local_config:
        exit_with_message('Local Settings not configured correctly, expecting "backup_dir"')

    backup_dir = local_config['backup_dir']

    if not os.path.isdir(backup_dir):
        exit_with_message('Directory [{}] does not exist.  Exiting ...'.format(backup_dir))

    backups = {}
    for item in os.listdir(backup_dir):
        backups[item] = os.path.join(backup_dir, item)

    return backups


def s3_backups(s3_config):
    raise NotImplementedError


def generate_unique_name(backup_dir, environemnt):
    # generate_file_name
    # database_name__2013_03_01.dmp.zip
    # or if that exists
    # database_name__2013_03_01(2).dmp.zip
    from datetime import datetime
    name_attempt = "{}__{}.dmp.zip".format(environemnt['db_name'], datetime.utcnow().strftime("%Y_%m_%d"))

    # check if file exists
    name_attemp_full_path = os.path.join(backup_dir, name_attempt)

    if not os.path.exists(name_attemp_full_path):
        return name_attemp_full_path
    else:
        counter = 1
        while True:
            counter += 1
            name_attempt = "{}__{}_{}.dmp.zip".format(environemnt['db_name'], datetime.utcnow().strftime("%Y_%m_%d"), counter)
            name_attempt_full_path = os.path.join(backup_dir, name_attempt)
            if os.path.exists(name_attempt_full_path):
                continue
            else:
                return name_attempt_full_path


def camel_to_underscore(name):
    return CAMEL_PAT.sub(lambda x: '_' + x.group(1).lower(), name)


def underscore_to_camel(name):
    return UNDER_PAT.sub(lambda x: x.group(1).upper(), name.capitalize())


def generate_migration_name(folder, name):
    # Can not start with a number so starting with a underscore
    rel_path = "{folder}/_{timestamp}_{name}_migration.py".format(
        folder=folder,
        timestamp=datetime.utcnow().strftime('%Y%m%d%H%M'),
        name=name
    )
    return os.path.abspath(rel_path)


def create_migration_directory_if_necessary(dir):
    try:
        os.makedirs(dir)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise

    try:
        with open(os.path.join(os.path.abspath(dir), '__init__.py'), 'w') as f:
            f.write('# this file makes migrations a package')
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
