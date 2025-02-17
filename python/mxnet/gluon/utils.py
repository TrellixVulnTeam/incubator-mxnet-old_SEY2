# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# coding: utf-8
# pylint: disable=
"""Parallelization utility optimizer."""
__all__ = ['split_data', 'split_and_load', 'clip_global_norm',
           'check_sha1', 'download']

import os
import hashlib
import warnings
import collections
import weakref
try:
    import requests
except ImportError:
    class requests_failed_to_import(object):
        pass
    requests = requests_failed_to_import

import numpy as np

from .. import ndarray

import gc

def split_data(data, num_slice, batch_axis=0, even_split=True):
    """Splits an NDArray into `num_slice` slices along `batch_axis`.
    Usually used for data parallelism where each slices is sent
    to one device (i.e. GPU).

    Parameters
    ----------
    data : NDArray
        A batch of data.
    num_slice : int
        Number of desired slices.
    batch_axis : int, default 0
        The axis along which to slice.
    even_split : bool, default True
        Whether to force all slices to have the same number of elements.
        If `True`, an error will be raised when `num_slice` does not evenly
        divide `data.shape[batch_axis]`.

    Returns
    -------
    list of NDArray
        Return value is a list even if `num_slice` is 1.
    """
    size = data.shape[batch_axis]
    if size < num_slice:
        raise ValueError(
            "Too many slices for data with shape %s. Arguments are " \
            "num_slice=%d and batch_axis=%d."%(str(data.shape), num_slice, batch_axis))
    if even_split and size % num_slice != 0:
        raise ValueError(
            "data with shape %s cannot be evenly split into %d slices along axis %d. " \
            "Use a batch size that's multiple of %d or set even_split=False to allow " \
            "uneven partitioning of data."%(
                str(data.shape), num_slice, batch_axis, num_slice))

    step = size // num_slice
    if batch_axis == 0:
        slices = [data[i*step:(i+1)*step] if i < num_slice - 1 else data[i*step:size]
                  for i in range(num_slice)]
    elif even_split:
        slices = ndarray.split(data, num_outputs=num_slice, axis=batch_axis)
    else:
        slices = [ndarray.slice_axis(data, batch_axis, i*step, (i+1)*step)
                  if i < num_slice - 1 else
                  ndarray.slice_axis(data, batch_axis, i*step, size)
                  for i in range(num_slice)]
    return slices


def split_and_load(data, ctx_list, batch_axis=0, even_split=True):
    """Splits an NDArray into `len(ctx_list)` slices along `batch_axis` and loads
    each slice to one context in `ctx_list`.

    Parameters
    ----------
    data : NDArray
        A batch of data.
    ctx_list : list of Context
        A list of Contexts.
    batch_axis : int, default 0
        The axis along which to slice.
    even_split : bool, default True
        Whether to force all slices to have the same number of elements.

    Returns
    -------
    list of NDArray
        Each corresponds to a context in `ctx_list`.
    """
    if not isinstance(data, ndarray.NDArray):
        data = ndarray.array(data, ctx=ctx_list[0])
    if len(ctx_list) == 1:
        return [data.as_in_context(ctx_list[0])]

    slices = split_data(data, len(ctx_list), batch_axis, even_split)
    return [i.as_in_context(ctx) for i, ctx in zip(slices, ctx_list)]

class PiplineLoader(object):
    def __init__(self, pool_size):
        self._pool_size = pool_size
        self._pool = []
        self._pool_counter = 0

    def split_and_load(self, data, ctx_list, batch_axis=0, even_split=True):
        if not isinstance(data, ndarray.NDArray):
            data = ndarray.array(data, ctx=ctx_list[0])
        
        if self._pool_counter < self._pool_size:
            if len(ctx_list) == 1:
                data_list = [data.as_in_context(ctx_list[0])]
            else:
                slices = split_data(data, len(ctx_list), batch_axis, even_split)
                data_list = [i.as_in_context(ctx) for i, ctx in zip(slices, ctx_list)]
            self._pool.append(data_list)
            
        else:
            data_list = self._pool[self._pool_counter % self._pool_size]
            if len(ctx_list) == 1:
                data.copyto(data_list[0])
            else:
                slices = split_data(data, len(data_list), batch_axis, even_split)
                for i, d in zip(slices, data_list):
                    i.copyto(d)
        self._pool_counter = self._pool_counter + 1
        if self._pool_counter == self._pool_size * 100:
            self._pool_counter = self._pool_size
        return data_list
    def reset(self):
        for data_list in self._pool:
            for d in data_list:
                del d
            del data_list
        self._pool = []
        self._pool_counter = 0
        gc.collect()

def clip_global_norm(arrays, max_norm):
    """Rescales NDArrays so that the sum of their 2-norm is smaller than `max_norm`.
    """
    def _norm(array):
        if array.stype == 'default':
            x = array.reshape((-1,))
            return ndarray.dot(x, x)
        return array.norm().square()
    assert len(arrays) > 0
    ctx = arrays[0].context
    total_norm = ndarray.add_n(*[_norm(arr).as_in_context(ctx) for arr in arrays])
    total_norm = ndarray.sqrt(total_norm).asscalar()
    if not np.isfinite(total_norm):
        warnings.warn(UserWarning('nan or inf is detected. Clipping results will be undefined.'),
                      stacklevel=2)
    scale = max_norm / (total_norm + 1e-8)
    if scale < 1.0:
        for arr in arrays:
            arr *= scale
    return total_norm


def _indent(s_, numSpaces):
    """Indent string
    """
    s = s_.split('\n')
    if len(s) == 1:
        return s_
    first = s.pop(0)
    s = [first] + [(numSpaces * ' ') + line for line in s]
    s = '\n'.join(s)
    return s


def check_sha1(filename, sha1_hash):
    """Check whether the sha1 hash of the file content matches the expected hash.

    Parameters
    ----------
    filename : str
        Path to the file.
    sha1_hash : str
        Expected sha1 hash in hexadecimal digits.

    Returns
    -------
    bool
        Whether the file content matches the expected hash.
    """
    sha1 = hashlib.sha1()
    with open(filename, 'rb') as f:
        while True:
            data = f.read(1048576)
            if not data:
                break
            sha1.update(data)

    return sha1.hexdigest() == sha1_hash


def download(url, path=None, overwrite=False, sha1_hash=None, retries=5, verify_ssl=True):
    """Download an given URL

    Parameters
    ----------
    url : str
        URL to download
    path : str, optional
        Destination path to store downloaded file. By default stores to the
        current directory with same name as in url.
    overwrite : bool, optional
        Whether to overwrite destination file if already exists.
    sha1_hash : str, optional
        Expected sha1 hash in hexadecimal digits. Will ignore existing file when hash is specified
        but doesn't match.
    retries : integer, default 5
        The number of times to attempt the download in case of failure or non 200 return codes
    verify_ssl : bool, default True
        Verify SSL certificates.

    Returns
    -------
    str
        The file path of the downloaded file.
    """
    if path is None:
        fname = url.split('/')[-1]
        # Empty filenames are invalid
        assert fname, 'Can\'t construct file-name from this URL. ' \
            'Please set the `path` option manually.'
    else:
        path = os.path.expanduser(path)
        if os.path.isdir(path):
            fname = os.path.join(path, url.split('/')[-1])
        else:
            fname = path
    assert retries >= 0, "Number of retries should be at least 0"

    if not verify_ssl:
        warnings.warn(
            'Unverified HTTPS request is being made (verify_ssl=False). '
            'Adding certificate verification is strongly advised.')

    if overwrite or not os.path.exists(fname) or (sha1_hash and not check_sha1(fname, sha1_hash)):
        dirname = os.path.dirname(os.path.abspath(os.path.expanduser(fname)))
        if not os.path.exists(dirname):
            os.makedirs(dirname)
        while retries+1 > 0:
            # Disable pyling too broad Exception
            # pylint: disable=W0703
            try:
                print('Downloading %s from %s...'%(fname, url))
                r = requests.get(url, stream=True, verify=verify_ssl)
                if r.status_code != 200:
                    raise RuntimeError("Failed downloading url %s"%url)
                with open(fname, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=1024):
                        if chunk: # filter out keep-alive new chunks
                            f.write(chunk)
                if sha1_hash and not check_sha1(fname, sha1_hash):
                    raise UserWarning('File {} is downloaded but the content hash does not match.'\
                                      ' The repo may be outdated or download may be incomplete. '\
                                      'If the "repo_url" is overridden, consider switching to '\
                                      'the default repo.'.format(fname))
                break
            except Exception as e:
                retries -= 1
                if retries <= 0:
                    raise e
                else:
                    print("download failed, retrying, {} attempt{} left"
                          .format(retries, 's' if retries > 1 else ''))

    return fname

def _get_repo_url():
    """Return the base URL for Gluon dataset and model repository."""
    default_repo = 'https://apache-mxnet.s3-accelerate.dualstack.amazonaws.com/'
    repo_url = os.environ.get('MXNET_GLUON_REPO', default_repo)
    if repo_url[-1] != '/':
        repo_url = repo_url+'/'
    return repo_url

def _get_repo_file_url(namespace, filename):
    """Return the URL for hosted file in Gluon repository.

    Parameters
    ----------
    namespace : str
        Namespace of the file.
    filename : str
        Name of the file
    """
    return '{base_url}{namespace}/{filename}'.format(base_url=_get_repo_url(),
                                                     namespace=namespace,
                                                     filename=filename)

def _brief_print_list(lst, limit=7):
    """Print at most `limit` elements of list."""
    lst = list(lst)
    if len(lst) > limit:
        return _brief_print_list(lst[:limit//2], limit) + ', ..., ' + \
            _brief_print_list(lst[-limit//2:], limit)
    return ', '.join(["'%s'"%str(i) for i in lst])


class HookHandle(object):
    """A handle that can attach/detach a hook."""

    def __init__(self):
        self._hooks_dict_ref = None
        self._id = None

    def attach(self, hooks_dict, hook):
        assert not self._hooks_dict_ref, 'The same handle cannot be attached twice.'
        self._id = id(hook)
        hooks_dict[self._id] = hook
        self._hooks_dict_ref = weakref.ref(hooks_dict)

    def detach(self):
        hooks_dict = self._hooks_dict_ref()
        if hooks_dict is not None and self._id in hooks_dict:
            del hooks_dict[self._id]

    def __getstate__(self):
        return (self._hooks_dict_ref(), self._id)

    def __setstate__(self, state):
        if state[0] is None:
            self._hooks_dict_ref = weakref.ref(collections.OrderedDict())
        else:
            self._hooks_dict_ref = weakref.ref(state[0])
        self._id = state[1]

    def __enter__(self):
        return self

    def __exit__(self, ptype, value, trace):
        self.detach()
