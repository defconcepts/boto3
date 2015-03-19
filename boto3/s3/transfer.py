"""Abstractions over S3's upload/download operations.

This module provides high level abstractions for efficient
uploads/downloads.  It handles several things for the user:

* Automatically switching to multipart transfers when
  a file is over a specific size threshold
* Uploading/downloading a file in parallel
* Throttling based on max bandwidth
* Progress callbacks to monitor transfers
* Retries.  While botocore handles retries for streaming uploads,
  it is not possible for it to handle retries for streaming
  downloads.  This module handles retries for both cases so
  you don't need to implement any retry logic yourself.

This module has a reasonable set of defaults.  It also allows you
to configure many aspects of the transfer process including:

* Multipart threshold size
* Max parallel downloads
* Max bandwidth
* Socket timeouts
* Retry amounts

There is no support for s3->s3 multipart copies at this
time.


Usage
=====

The simplest way to use this module is:

.. code-block:: python

    client = boto3.client('s3', 'us-west-2')
    transfer = S3Transfer(client)
    # Upload /tmp/myfile to s3://bucket/key
    transfer.upload_file('/tmp/myfile', 'bucket', 'key')

    # Download s3://bucket/key to /tmp/myfile
    transfer.download_file('bucket', 'key', '/tmp/myfile')

The ``upload_file`` and ``download_file`` methods also accept
``**kwargs``, which will be forwarded through to the corresponding
client operation.  Here are a few examples using ``upload_file``::

    # Making the object public
    transfer.upload_file('/tmp/myfile', 'bucket', 'key', ACL='public-read')

    # Setting metadata
    transfer.upload_file('/tmp/myfile', 'bucket', 'key',
                         Metadata={'a': 'b', 'c': 'd'})

    # Setting content type
    transfer.upload_file('/tmp/myfile.json', 'bucket', 'key',
                         ContentType="application/json")



The ``S3Transfer`` clas also supports progress callbacks so you can
provide transfer progress to users.  Both the ``upload_file`` and
``download_file`` methods take an optional ``callback`` parameter.
Here's an example of how to print a simple progress percentage
to the user:

.. code-block:: python

    class ProgressPercentage(object):
        def __init__(self, filename):
            self._filename = filename
            self._size = float(os.path.getsize(filename))
            self._seen_so_far = 0
            self._lock = threading.Lock()

        def __call__(self, filename, bytes_amount):
            # To simplify we'll assume this is hooked up
            # to a single filename.
            with self._lock:
                self._seen_so_far += bytes_amount
                percentage = (self._seen_so_far / self._size) * 100
                sys.stdout.write(
                    "\r%s  %s / %s  (%.2f%%)" % (filename, self._seen_so_far,
                                                 self._size, percentage))
                sys.stdout.flush()


    transfer = S3Transfer(boto3.client('s3', 'us-west-2'))
    # Upload /tmp/myfile to s3://bucket/key and print upload progress.
    transfer.upload_file('/tmp/myfile', 'bucket', 'key',
                         callback=ProgressPercentage('/tmp/myfile'))



You can also provide an TransferConfig object to the S3Transfer
object that gives you more fine grained control over the
transfer.  For example:

.. code-block:: python

    client = boto3.client('s3', 'us-west-2')
    config = TransferConfig(
        multipart_threshold=8 * 1024 * 1024,
        max_concurrency=10,
        max_retries=10,
        socket_timeout=120,
    )
    transfer = S3Transfer(client, config)
    transfer.upload_file('/tmp/foo', 'bucket', 'key')


"""
import os
import math
import threading
import functools
from concurrent import futures


MB = 1024 * 1024


class ReadFileChunk(object):
    def __init__(self, fileobj, start_byte, chunk_size, full_file_size,
                 callback=None):
        """

        Given a file object shown below:

            |___________________________________________________|
            0          |                 |                 full_file_size
                       |----chunk_size---|
                 start_byte

        :type fileobj: file
        :param fileobj: File like object

        :type start_byte: int
        :param start_byte: The first byte from which to start reading.

        :type chunk_size: int
        :param chunk_size: The max chunk size to read.  Trying to read
            pass the end of the chunk size will behave like you've
            reached the end of the file.

        :type full_file_size: int
        :param full_file_size: The entire content length associated
            with ``fileobj``.

        :type callback: function(amount_read)
        :param callback: Called whenever data is read from this object.

        """
        self._fileobj = fileobj
        self._start_byte = start_byte
        self._size = self._calculate_file_size(
            self._fileobj, requested_size=chunk_size,
            start_byte=start_byte, actual_file_size=full_file_size)
        self._fileobj.seek(self._start_byte)
        self._amount_read = 0
        self._callback = callback

    @classmethod
    def from_filename(cls, filename, start_byte, chunk_size, callback=None):
        """Convenience factory function to create from a filename."""
        f = open(filename, 'rb')
        file_size = os.fstat(f.fileno()).st_size
        return cls(f, start_byte, chunk_size, file_size, callback)

    def _calculate_file_size(self, fileobj, requested_size, start_byte,
                             actual_file_size):
        max_chunk_size = actual_file_size - start_byte
        return min(max_chunk_size, requested_size)

    def read(self, amount=None):
        if amount is None:
            amount_to_read = self._size - self._amount_read
        else:
            amount_to_read = min(self._size - self._amount_read, amount)
        data = self._fileobj.read(amount_to_read)
        self._amount_read += len(data)
        if self._callback is not None:
            self._callback(len(data))
        return data

    def seek(self, where):
        self._fileobj.seek(self._start_byte + where)
        self._amount_read = where

    def close(self):
        self._fileobj.close()

    def tell(self):
        return self._amount_read

    def __len__(self):
        # __len__ is defined because requests will try to determine the length
        # of the stream to set a content length.  In the normal case
        # of the file it will just stat the file, but we need to change that
        # behavior.  By providing a __len__, requests will use that instead
        # of stat'ing the file.
        return self._size

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        self.close()

    def __iter__(self):
        # This is a workaround for http://bugs.python.org/issue17575
        # Basically httplib will try to iterate over the contents, even
        # if its a file like object.  This wasn't noticed because we've
        # already exhausted the stream so iterating over the file immediately
        # steps, which is what we're simulating here.
        return iter([])


class StreamReaderProgress(object):
    """Wrapper for a read only stream that adds progress callbacks."""
    def __init__(self, stream, callback=None):
        self._stream = stream
        self._callback = callback

    def read(self, *args, **kwargs):
        value = self._stream.read(*args, **kwargs)
        if self._callback is not None:
            self._callback(len(value))
        return value


class ThreadSafeWriter(object):
    def __init__(self, write_stream):
        self._write_stream = write_stream
        self._lock = threading.Lock()

    def pwrite(self, data, offset):
        with self._lock:
            self._write_stream.seek(offset)
            self._write_stream.write(data)

    def close(self):
        return self._write_stream.close()


class OSUtils(object):
    def get_file_size(self, filename):
        return os.path.getsize(filename)

    def open_file_chunk_reader(self, filename, start_byte, size, callback):
        return ReadFileChunk.from_filename(filename, start_byte,
                                           size, callback)

    def open(self, filename, mode):
        return open(filename, mode)

    def wrap_stream_with_callback(self, stream, callback):
        return StreamReaderProgress(stream, callback)

    def wrap_thread_safe_writer(self, stream):
        return ThreadSafeWriter(stream)


class MultipartUploader(object):
    def __init__(self, client, config, osutil):
        self._client = client
        self._config = config
        self._os = osutil

    def upload_file(self, filename, bucket, key, callback, extra_args):
        response = self._client.create_multipart_upload(Bucket=bucket,
                                                        Key=key, **extra_args)
        upload_id = response['UploadId']
        parts = []
        part_size = self._config.multipart_chunksize
        num_parts = int(
            math.ceil(self._os.get_file_size(filename) / float(part_size)))
        max_workers = self._config.max_concurrency
        with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            upload_partial = functools.partial(
                self._upload_one_part, filename, bucket, key, upload_id,
                part_size, callback)
            for part in executor.map(upload_partial, range(1, num_parts + 1)):
                parts.append(part)
        # Parts have to be ordered by part number.
        parts.sort(key=lambda x: x['PartNumber'])
        self._client.complete_multipart_upload(
            Bucket=bucket, Key=key, UploadId=upload_id,
            MultipartUpload={'Parts': parts})

    def _upload_one_part(self, filename, bucket, key,
                         upload_id, part_size, callback, part_number):
        open_chunk_reader = self._os.open_file_chunk_reader
        with open_chunk_reader(filename, part_size * (part_number - 1),
                               part_size, callback) as body:
            response = self._client.upload_part(
                Bucket=bucket, Key=key,
                UploadId=upload_id, PartNumber=part_number, Body=body)
            etag = response['ETag']
            return {'ETag': etag, 'PartNumber': part_number}


class MultipartDownloaded(object):
    def __init__(self, client, config, osutil):
        self._client = client
        self._config = config
        self._os = osutil

    def download_file(self, bucket, key, filename, object_size,
                      callback=None):
        part_size = self._config.multipart_chunksize
        num_parts = int(math.ceil(object_size / float(part_size)))
        max_workers = self._config.max_concurrency
        with open(filename, 'wb') as f:
            download_partial = functools.partial(
                self._download_range, bucket, key, filename,
                part_size, num_parts, callback, f)
            with futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                list(executor.map(download_partial, range(num_parts)))

    def _download_range(self, bucket, key, filename,
                        part_size, num_parts, callback, fileobj, i):
        start_range = i * part_size
        if i == num_parts - 1:
            end_range = ''
        else:
            end_range = start_range + part_size - 1
        range_param = 'bytes=%s-%s' % (start_range, end_range)
        response = self._client.get_object(
            Bucket=bucket, Key=key, Range=range_param)
        streaming_body = self._os.wrap_stream_with_callback(
            response['Body'], callback)
        buffer_size = 1024 * 16
        current_index = start_range
        safe_writer = self._os.wrap_thread_safe_writer(fileobj)
        for chunk in iter(lambda: streaming_body.read(buffer_size), b''):
            safe_writer.pwrite(chunk, current_index)
            current_index += len(chunk)


class TransferConfig(object):
    def __init__(self,
                 multipart_threshold=8 * MB,
                 max_concurrency=1,
                 multipart_chunksize=8 * MB):
        self.multipart_threshold = multipart_threshold
        self.max_concurrency = max_concurrency
        self.multipart_chunksize = multipart_chunksize


class S3Transfer(object):

    def __init__(self, client, config=None, osutil=None):
        self._client = client
        if config is None:
            config = TransferConfig()
        self._config = config
        if osutil is None:
            osutil = OSUtils()
        self._osutil = osutil

    def upload_file(self, filename, bucket, key,
                    callback=None, extra_args=None):
        if extra_args is None:
            extra_args = {}
        if self._osutil.get_file_size(filename) >= \
                self._config.multipart_threshold:
            self._multipart_upload(filename, bucket, key, callback, extra_args)
        else:
            self._put_object(filename, bucket, key, callback, extra_args)

    def _put_object(self, filename, bucket, key, callback, extra_args):
        # We're using open_file_chunk_reader so we can take advantage of the
        # progress callback functionality.
        open_chunk_reader = self._osutil.open_file_chunk_reader
        with open_chunk_reader(filename, 0,
                               self._osutil.get_file_size(filename),
                               callback=callback) as body:
            self._client.put_object(Bucket=bucket, Key=key, Body=body,
                                    **extra_args)

    def download_file(self, bucket, key, filename, callback=None):
        """Download an S3 object to a file.

        This method will issue a ``head_object`` request to determine
        the size of the S3 object.  This is used to determine if the
        object is downloaded in parallel.

        """
        object_size = self._object_size(bucket, key)
        if object_size >= self._config.multipart_threshold:
            self._ranged_download(bucket, key, filename, object_size, callback)
        else:
            self._get_object(bucket, key, filename, callback)

    def _ranged_download(self, bucket, key, filename, object_size, callback):
        downloader = MultipartDownloaded(self._client, self._config,
                                         self._osutil)
        downloader.download_file(bucket, key, filename, object_size,
                                 callback)

    def _get_object(self, bucket, key, filename, callback):
        response = self._client.get_object(Bucket=bucket, Key=key)
        # TODO: we need retries here.  While botocore will retry the
        # get_object() request, once we are handed the streaming body,
        # it's on us to retry this appropriately if the connection
        # is reset halfway through.
        streaming_body = self._osutil.wrap_stream_with_callback(
            response['Body'], callback)
        with self._osutil.open(filename, 'wb') as f:
            for chunk in iter(lambda: streaming_body.read(8192), b''):
                f.write(chunk)

    def _object_size(self, bucket, key):
        return self._client.head_object(
            Bucket=bucket, Key=key)['ContentLength']

    def _multipart_upload(self, filename, bucket, key, callback, extra_args):
        uploader = MultipartUploader(self._client, self._config, self._osutil)
        uploader.upload_file(filename, bucket, key, callback, extra_args)


if __name__ == '__main__':
    import boto3
    import sys
    import time

    class ProgressPercentage(object):
        def __init__(self, filename, size=None):
            self._filename = filename
            if size is None:
                size = float(os.path.getsize(filename))
            self._size = size
            self._seen_so_far = 0
            self._lock = threading.Lock()
            self._counter = 0
            self._start_time = None

        def __call__(self, bytes_amount):
            # To simplify we'll assume this is hooked up
            # to a single filename.
            if self._start_time is None:
                self._start_time = time.time()
            with self._lock:
                self._seen_so_far += bytes_amount
                self._counter += 1
                percentage = (self._seen_so_far / self._size) * 100
                rate = self._seen_so_far / (time.time() - self._start_time)
                # convert to Mbps
                rate = rate / (10 ** 6)
                if self._counter % 10 == 0 or percentage == 100:
                    sys.stdout.write(
                        "\r%s  %s / %s  (%.2f%%) (%.4f Mbps)" % (
                            self._filename, self._seen_so_far,
                            self._size, percentage, rate))
                    sys.stdout.flush()
    transfer = S3Transfer(boto3.client('s3', 'us-west-2'))
    #transfer.upload_file(
    #    '/tmp/largefile', 'jamesls-test-sync',
    #    'largefile', ProgressPercentage('/tmp/largefile'))
    #transfer.upload_file('/tmp/mediumfile', 'jamesls-test-sync',
    #                     'mediumfile',
    #                     ProgressPercentage('/tmp/mediumfile'),
    #                     ACL='public-read',
    #                     ContentType='application/json')
    transfer.download_file(
        'jamesls-test-sync', '100mb', '/tmp/downloaded100mb',
    ProgressPercentage('jamesls-test-sync/100mb', size=1024 * 1024 * 100))
    print("\n")