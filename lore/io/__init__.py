import logging
import os
import pickle
import re
import shutil
import tarfile
import tempfile
try:
    from urllib.parse import urlparse
except ImportError:
    from urlparse import urlparse

import lore
from lore.env import require, configparser
from lore.util import timer
from lore.io.connection import Connection
from lore.io.multi_connection_proxy import MultiConnectionProxy


logger = logging.getLogger(__name__)


config = lore.env.DATABASE_CONFIG
if config:
    try:
        for database, url in config.items('DATABASES'):
            vars()[database] = Connection(url=url, name=database)
    except configparser.NoSectionError:
        pass

    for section in config.sections():
        if section == 'DATABASES':
            continue

        options = config._sections[section]
        if options.get('url') == '$DATABASE_URL':
            logger.error('$DATABASE_URL is not set, but is used in config/database.cfg. Skipping connection.')
        else:
            if 'urls' in options:
                vars()[section.lower()] = MultiConnectionProxy(name=section.lower(), **options)
            else:
                vars()[section.lower()] = Connection(name=section.lower(), **options)


if 'metadata' not in vars():
    vars()['metadata'] = Connection('sqlite:///%s/metadata.sqlite' % lore.env.DATA_DIR)

redis_config = lore.env.REDIS_CONFIG
if redis_config:
    require(lore.dependencies.REDIS)
    import redis

    for section in redis_config.sections():
        vars()[section.lower()] = redis.StrictRedis(host=redis_config.get(section, 'url'),
                                                    port=redis_config.get(section, 'port'))

s3 = None
bucket = None
if lore.env.AWS_CONFIG:
    require(lore.dependencies.S3)
    import boto3
    from botocore.exceptions import ClientError

    config = lore.env.AWS_CONFIG
    if config and 'ACCESS_KEY' in config.sections():
        s3 = boto3.resource(
            's3',
            aws_access_key_id=config.get('ACCESS_KEY', 'id'),
            aws_secret_access_key=config.get('ACCESS_KEY', 'secret')
        )
    else:
        s3 = boto3.resource('s3')

    if s3 and config and 'BUCKET' in config.sections():
        bucket = s3.Bucket(config.get('BUCKET', 'name'))


def download(remote_url, local_path=None, cache=True, extract=False):
    _bucket = bucket
    if re.match(r'^https?://', remote_url):
        protocol = 'http'
    elif re.match(r'^s3?://', remote_url):
        require(lore.dependencies.S3)
        import boto3
        from botocore.exceptions import ClientError
        protocol = 's3'
        url_parts = urlparse(remote_url)
        remote_url = url_parts.path[1:]
        _bucket = boto3.resource('s3').Bucket(url_parts.netloc)
    else:
        if s3 is None or bucket is None:
            raise NotImplementedError("Cannot download from s3 without config/aws.cfg")
        protocol = 's3'
        remote_url = prefix_remote_root(remote_url)
    if cache:
        if local_path is None:
            if protocol == 'http':
                filename = lore.env.parse_url(remote_url).path.split('/')[-1]
            elif protocol == 's3':
                filename = remote_url
            local_path = os.path.join(lore.env.DATA_DIR, filename)

        if os.path.exists(local_path):
            return local_path
    elif local_path:
        raise ValueError("You can't pass lore.io.download(local_path=X), unless you also pass cache=True")
    elif extract:
        raise ValueError("You can't pass lore.io.download(extract=True), unless you also pass cache=True")

    with timer('DOWNLOAD: %s' % remote_url):
        temp_file, temp_path = tempfile.mkstemp(dir=lore.env.WORK_DIR)
        os.close(temp_file)
        try:
            if protocol == 'http':
                lore.env.retrieve_url(remote_url, temp_path)
            else:
                _bucket.download_file(remote_url, temp_path)
        except ClientError as e:
            logger.error("Error downloading file: %s" % e)
            raise

    if cache:
        dir = os.path.dirname(local_path)
        if not os.path.exists(dir):
            try:
                os.makedirs(dir)
            except os.FileExistsError:
                pass  # race to create

        shutil.copy(temp_path, local_path)
        os.remove(temp_path)

        if extract:
            with timer('EXTRACT: %s' % local_path, logging.WARNING):
                if local_path[-7:] == '.tar.gz':
                    with tarfile.open(local_path, 'r:gz') as tar:
                        tar.extractall(os.path.dirname(local_path))
                elif local_path[-4:] == '.zip':
                    import zipfile
                    with zipfile.ZipFile(local_path, 'r') as zip:
                        zip.extractall(os.path.dirname(local_path))

    else:
        local_path = temp_path
    return local_path


# Note: This can be rewritten in a more efficient way
# https://stackoverflow.com/questions/11426560/amazon-s3-boto-how-to-delete-folder
def delete_folder(remote_url):
    if remote_url is None:
        raise ValueError("remote_url cannot be None")
    else:
        remote_url = prefix_remote_root(remote_url)
        if not remote_url.endswith('/'):
            remote_url = remote_url + '/'
        keys = bucket.objects.filter(Prefix=remote_url)
        empty = True

        for key in keys:
            empty = False
            key.delete()

        if empty:
            logger.info('Remote was not a folder')


def delete(remote_url, recursive=False):
    if s3 is None:
        raise NotImplementedError("Cannot delete from s3 without config/aws.cfg")

    if remote_url is None:
        raise ValueError("remote_url cannot be None")

    if (recursive is False) and (remote_url.endswith('/')):
        raise ValueError("remote_url cannot end with trailing / when recursive is False")

    remote_url = prefix_remote_root(remote_url)
    if recursive is True:
        delete_folder(remote_url)
    else:
        obj = bucket.Object(key=remote_url)
        obj.delete()


def upload_object(obj, remote_path=None):
    if remote_path is None:
        raise ValueError("remote_path cannot be None when uploading objects")
    else:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            pickle.dump(obj, f)
        upload_file(f.name, remote_path)
        os.remove(f.name)


def upload_file(local_path, remote_path=None):
    if s3 is None:
        raise NotImplementedError("Cannot upload to s3 without config/aws.cfg")

    if remote_path is None:
        remote_path = remote_from_local(local_path)
    remote_path = prefix_remote_root(remote_path)

    with timer('UPLOAD: %s -> %s' % (local_path, remote_path)):
        try:
            bucket.upload_file(local_path, remote_path, ExtraArgs={'ServerSideEncryption': 'AES256'})
        except ClientError as e:
            logger.error("Error uploading file: %s" % e)
            raise


def upload(obj, remote_path=None):
    if isinstance(obj, str):
        local_path = obj
        upload_file(local_path, remote_path)
    else:
        upload_object(obj, remote_path)


def remote_from_local(local_path):
    return re.sub(
        r'^%s' % re.escape(lore.env.WORK_DIR),
        '',
        local_path
    )


def prefix_remote_root(path):
    if path.startswith('/'):
        path = path[1:]

    if not path.startswith(lore.env.NAME + '/'):
        path = os.path.join(lore.env.NAME, path)

    return path
