"""
License:
This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
If a copy of the MPL was not distributed with this file, You can obtain one at https://mozilla.org/MPL/2.0/.
"""

import os
import posixpath
import collections.abc as abc
import json
import sys
import traceback
from collections import defaultdict
import numpy as np
from PIL import Image as im, ImageChops

import fsspec
from fsspec.spec import AbstractFileSystem
import numcodecs
import numcodecs.lz4
import numcodecs.zstd
import numpy as np

from hub.schema.features import (
    Primitive,
    Tensor,
    SchemaDict,
    HubSchema,
    featurify,
)
from hub.log import logger
import hub.store.pickle_s3_storage

from hub.api.datasetview import DatasetView
from hub.api.objectview import ObjectView
from hub.api.tensorview import TensorView
from hub.api.dataset_utils import (
    create_numpy_dict,
    get_value,
    slice_split,
    str_to_int,
    _copy_helper,
)

import hub.schema.serialize
import hub.schema.deserialize
from hub.schema.features import flatten
from hub.schema import ClassLabel

from hub.store.dynamic_tensor import DynamicTensor
from hub.store.store import get_fs_and_path, get_storage_map
from hub.exceptions import (
    HubDatasetNotFoundException,
    NotHubDatasetToOverwriteException,
    NotHubDatasetToAppendException,
    OutOfBoundsError,
    ShapeArgumentNotFoundException,
    SchemaArgumentNotFoundException,
    ModuleNotInstalledException,
    ShapeLengthException,
    WrongUsernameException,
)
from hub.store.metastore import MetaStorage
from hub.client.hub_control import HubControlClient
from hub.schema import Audio, BBox, ClassLabel, Image, Sequence, Text, Video
from hub.numcodecs import PngCodec

from hub.utils import norm_cache, norm_shape, _tuple_product
from hub import defaults


def get_file_count(fs: fsspec.AbstractFileSystem, path):
    return len(fs.listdir(path, detail=False))


class Dataset:
    def __init__(
        self,
        url: str,
        mode: str = None,
        shape=None,
        schema=None,
        token=None,
        fs=None,
        fs_map=None,
        meta_information=dict(),
        cache: int = defaults.DEFAULT_MEMORY_CACHE_SIZE,
        storage_cache: int = defaults.DEFAULT_STORAGE_CACHE_SIZE,
        lock_cache=True,
        tokenizer=None,
        lazy: bool = True,
        public: bool = True,
        name: str = None,
    ):
        """| Open a new or existing dataset for read/write

        Parameters
        ----------
        url: str
            The url where dataset is located/should be created
        mode: str, optional (default to "a")
            Python way to tell whether dataset is for read or write (ex. "r", "w", "a")
        shape: tuple, optional
            Tuple with (num_samples,) format, where num_samples is number of samples
        schema: optional
            Describes the data of a single sample. Hub schemas are used for that
            Required for 'a' and 'w' modes
        token: str or dict, optional
            If url is refering to a place where authorization is required,
            token is the parameter to pass the credentials, it can be filepath or dict
        fs: optional
        fs_map: optional
        meta_information: optional ,give information about dataset in a dictionary.
        cache: int, optional
            Size of the memory cache. Default is 64MB (2**26)
            if 0, False or None, then cache is not used
        storage_cache: int, optional
            Size of the storage cache. Default is 256MB (2**28)
            if 0, False or None, then storage cache is not used
        lock_cache: bool, optional
            Lock the cache for avoiding multiprocessing errors
        lazy: bool, optional
            Setting this to False will stop lazy computation and will allow items to be accessed without .compute()
        public: bool, optional
            only applicable if using hub storage, ignored otherwise
            setting this to False allows only the user who created it to access the dataset and
            the dataset won't be visible in the visualizer to the public
        name: str, optional
            only applicable when using hub storage, this is the name that shows up on the visualizer
        """

        shape = norm_shape(shape)
        if len(shape) != 1:
            raise ShapeLengthException()

        storage_cache = norm_cache(storage_cache) if cache else 0
        cache = norm_cache(cache)
        schema: SchemaDict = featurify(schema) if schema else None

        self._url = url
        self._token = token
        self.tokenizer = tokenizer
        self.lazy = lazy
        self._name = name

        self._fs, self._path = (
            (fs, url) if fs else get_fs_and_path(self._url, token=token, public=public)
        )
        self._cache = cache
        self._storage_cache = storage_cache
        self.lock_cache = lock_cache
        self.verison = "1.x"
        mode = self._get_mode(mode, self._fs)
        self._mode = mode
        needcreate = self._check_and_prepare_dir()
        fs_map = fs_map or get_storage_map(
            self._fs, self._path, cache, lock=lock_cache, storage_cache=storage_cache
        )
        self._fs_map = fs_map
        self._meta_information = meta_information
        self.username = None
        self.dataset_name = None
        if not needcreate:
            self.meta = json.loads(fs_map["meta.json"].decode("utf-8"))
            self._name = self.meta.get("name") or None
            self._shape = tuple(self.meta["shape"])
            self._schema = hub.schema.deserialize.deserialize(self.meta["schema"])
            self._meta_information = self.meta.get("meta_info") or dict()
            self._flat_tensors = tuple(flatten(self._schema))
            self._tensors = dict(self._open_storage_tensors())
            if shape != (None,) and shape != self._shape:
                raise TypeError(
                    f"Shape in metafile [{self._shape}]  and shape in arguments [{shape}] are !=, use mode='w' to overwrite dataset"
                )
            if schema is not None and sorted(schema.dict_.keys()) != sorted(
                self._schema.dict_.keys()
            ):
                raise TypeError(
                    "Schema in metafile and schema in arguments do not match, use mode='w' to overwrite dataset"
                )

        else:
            if shape[0] is None:
                raise ShapeArgumentNotFoundException()
            if schema is None:
                raise SchemaArgumentNotFoundException()
            try:
                if shape is None:
                    raise ShapeArgumentNotFoundException()
                if schema is None:
                    raise SchemaArgumentNotFoundException()
                self._schema = schema
                self._shape = tuple(shape)
                self.meta = self._store_meta()
                self._meta_information = meta_information
                self._flat_tensors = tuple(flatten(self.schema))
                self._tensors = dict(self._generate_storage_tensors())
                self.flush()
            except Exception as e:
                try:
                    self.close()
                except Exception:
                    pass
                self._fs.rm(self._path, recursive=True)
                logger.error("Deleting the dataset " + traceback.format_exc() + str(e))
                raise

        self.indexes = list(range(self._shape[0]))

        if self._path.startswith("s3://snark-hub-dev/") or self._path.startswith(
            "s3://snark-hub/"
        ):
            subpath = self._path[5:]
            spl = subpath.split("/")
            if len(spl) < 4:
                raise ValueError("Invalid Path for dataset")
            self.username = spl[-2]
            self.dataset_name = spl[-1]
            if needcreate:
                HubControlClient().create_dataset_entry(
                    self.username, self.dataset_name, self.meta, public=public
                )

    @property
    def mode(self):
        return self._mode

    @property
    def url(self):
        return self._url

    @property
    def shape(self):
        return self._shape

    @property
    def name(self):
        return self._name

    @property
    def token(self):
        return self._token

    @property
    def cache(self):
        return self._cache

    @property
    def storage_cache(self):
        return self._storage_cache

    @property
    def schema(self):
        return self._schema

    @property
    def meta_information(self):
        return self._meta_information

    def _store_meta(self) -> dict:
        meta = {
            "shape": self._shape,
            "schema": hub.schema.serialize.serialize(self._schema),
            "version": 1,
            "meta_info": self._meta_information or dict(),
            "name": self._name,
        }

        self._fs_map["meta.json"] = bytes(json.dumps(meta), "utf-8")
        return meta

    def _check_and_prepare_dir(self):
        """
        Checks if input data is ok.
        Creates or overwrites dataset folder.
        Returns True dataset needs to be created opposed to read.
        """
        fs, path, mode = self._fs, self._path, self._mode
        if path.startswith("s3://"):
            with open(os.path.expanduser("~/.activeloop/store"), "rb") as f:
                stored_username = json.load(f)["_id"]
            current_username = path.split("/")[-2]
            if stored_username != current_username:
                try:
                    fs.listdir(path)
                except BaseException:
                    raise WrongUsernameException(stored_username)
        meta_path = posixpath.join(path, "meta.json")
        try:
            # Update boto3 cache
            fs.ls(path, detail=False, refresh=True)
        except Exception:
            pass
        exist_meta = fs.exists(meta_path)
        if exist_meta:
            if "w" in mode:
                fs.rm(path, recursive=True)
                fs.makedirs(path)
                return True
            return False
        else:
            if "r" in mode:
                raise HubDatasetNotFoundException(path)
            exist_dir = fs.exists(path)
            if not exist_dir:
                fs.makedirs(path)
            elif get_file_count(fs, path) > 0:
                if "w" in mode:
                    raise NotHubDatasetToOverwriteException()
                else:
                    raise NotHubDatasetToAppendException()
            return True

    def _get_dynamic_tensor_dtype(self, t_dtype):
        if isinstance(t_dtype, Primitive):
            return t_dtype.dtype
        elif isinstance(t_dtype.dtype, Primitive):
            return t_dtype.dtype.dtype
        else:
            return "object"

    def _get_compressor(self, compressor: str):
        if compressor.lower() == "lz4":
            return numcodecs.LZ4(numcodecs.lz4.DEFAULT_ACCELERATION)
        elif compressor.lower() == "zstd":
            return numcodecs.Zstd(numcodecs.zstd.DEFAULT_CLEVEL)
        elif compressor.lower() == "default":
            return "default"
        elif compressor.lower() == "png":
            return PngCodec(solo_channel=True)
        else:
            raise ValueError(
                f"Wrong compressor: {compressor}, only LZ4 and ZSTD are supported"
            )

    def _generate_storage_tensors(self):
        for t in self._flat_tensors:
            t_dtype, t_path = t
            path = posixpath.join(self._path, t_path[1:])
            self._fs.makedirs(posixpath.join(path, "--dynamic--"))
            yield t_path, DynamicTensor(
                fs_map=MetaStorage(
                    t_path,
                    get_storage_map(
                        self._fs,
                        path,
                        self._cache,
                        self.lock_cache,
                        storage_cache=self._storage_cache,
                    ),
                    self._fs_map,
                ),
                mode=self._mode,
                shape=self._shape + t_dtype.shape,
                max_shape=self._shape + t_dtype.max_shape,
                dtype=self._get_dynamic_tensor_dtype(t_dtype),
                chunks=t_dtype.chunks,
                compressor=self._get_compressor(t_dtype.compressor),
            )

    def _open_storage_tensors(self):
        for t in self._flat_tensors:
            t_dtype, t_path = t
            path = posixpath.join(self._path, t_path[1:])
            yield t_path, DynamicTensor(
                fs_map=MetaStorage(
                    t_path,
                    get_storage_map(
                        self._fs,
                        path,
                        self._cache,
                        self.lock_cache,
                        storage_cache=self._storage_cache,
                    ),
                    self._fs_map,
                ),
                mode=self._mode,
                # FIXME We don't need argument below here
                shape=self._shape + t_dtype.shape,
            )

    def __getitem__(self, slice_):
        """| Gets a slice or slices from dataset
        | Usage:
        >>> return ds["image", 5, 0:1920, 0:1080, 0:3].compute() # returns numpy array
        >>> images = ds["image"]
        >>> return images[5].compute() # returns numpy array
        >>> images = ds["image"]
        >>> image = images[5]
        >>> return image[0:1920, 0:1080, 0:3].compute()
        """
        if not isinstance(slice_, abc.Iterable) or isinstance(slice_, str):
            slice_ = [slice_]
        slice_ = list(slice_)
        subpath, slice_list = slice_split(slice_)
        if not subpath:
            if len(slice_list) > 1:
                raise ValueError(
                    "Can't slice a dataset with multiple slices without key"
                )
            indexes = self.indexes[slice_list[0]]
            return DatasetView(
                dataset=self,
                indexes=indexes,
                lazy=self.lazy,
            )
        elif not slice_list:
            if subpath in self.keys:
                tensorview = TensorView(
                    dataset=self,
                    subpath=subpath,
                    slice_=slice(0, self._shape[0]),
                    lazy=self.lazy,
                )
                return tensorview if self.lazy else tensorview.compute()
            for key in self.keys:
                if subpath.startswith(key):
                    objectview = ObjectView(
                        dataset=self,
                        subpath=subpath,
                        lazy=self.lazy,
                        slice_=[slice(0, self._shape[0])],
                    )
                    return objectview if self.lazy else objectview.compute()
            return self._get_dictionary(subpath)
        else:
            schema_obj = self.schema.dict_[subpath.split("/")[1]]
            if subpath in self.keys and (
                not isinstance(schema_obj, Sequence) or len(slice_list) <= 1
            ):
                tensorview = TensorView(
                    dataset=self, subpath=subpath, slice_=slice_list, lazy=self.lazy
                )
                return tensorview if self.lazy else tensorview.compute()
            for key in self.keys:
                if subpath.startswith(key):
                    objectview = ObjectView(
                        dataset=self,
                        subpath=subpath,
                        slice_=slice_list,
                        lazy=self.lazy,
                    )
                    return objectview if self.lazy else objectview.compute()
            if len(slice_list) > 1:
                raise ValueError("You can't slice a dictionary of Tensors")
            return self._get_dictionary(subpath, slice_list[0])

    def __setitem__(self, slice_, value):
        """| Sets a slice or slices with a value
        | Usage:
        >>> ds["image", 5, 0:1920, 0:1080, 0:3] = np.zeros((1920, 1080, 3), "uint8")
        >>> images = ds["image"]
        >>> image = images[5]
        >>> image[0:1920, 0:1080, 0:3] = np.zeros((1920, 1080, 3), "uint8")
        """
        assign_value = get_value(value)
        # handling strings and bytes
        assign_value = str_to_int(assign_value, self.tokenizer)

        if not isinstance(slice_, abc.Iterable) or isinstance(slice_, str):
            slice_ = [slice_]
        slice_ = list(slice_)
        subpath, slice_list = slice_split(slice_)

        if not subpath:
            raise ValueError("Can't assign to dataset sliced without subpath")
        elif subpath not in self.keys:
            raise KeyError(f"Key {subpath} not found in the dataset")

        if not slice_list:
            self._tensors[subpath][:] = assign_value
        else:
            self._tensors[subpath][slice_list] = assign_value

    def filter(self, fn):
        """| Applies a function on each element one by one as a filter to get a new DatasetView

        Parameters
        ----------
        fn: function
            Should take in a single sample of the dataset and return True or False
            This function is applied to all the items of the datasetview and retains those items that return True
        """
        indexes = [index for index in self.indexes if fn(self[index])]
        return DatasetView(dataset=self, lazy=self.lazy, indexes=indexes)

    def copy(self, dst_url: str, token=None, fs=None, public=True):
        """| Creates a copy of the dataset at the specified url and returns the dataset object
        Parameters
        ----------
        dst_url: str
            The destination url where dataset should be copied
        token: str or dict, optional
            If dst_url is refering to a place where authorization is required,
            token is the parameter to pass the credentials, it can be filepath or dict
        fs: optional
        public: bool, optional
            only applicable if using hub storage, ignored otherwise
            setting this to False allows only the user who created it to access the new copied dataset and
            the dataset won't be visible in the visualizer to the public
        """
        self.flush()
        destination = dst_url
        path = _copy_helper(
            dst_url=dst_url,
            token=token,
            fs=fs,
            public=public,
            src_url=self._path,
            src_fs=self._fs,
        )

        #  create entry in database if stored in hub storage
        if path.startswith("s3://snark-hub-dev/") or path.startswith("s3://snark-hub/"):
            subpath = path[5:]
            spl = subpath.split("/")
            if len(spl) < 4:
                raise ValueError("Invalid Path for dataset")
            username = spl[-2]
            dataset_name = spl[-1]
            HubControlClient().create_dataset_entry(
                username, dataset_name, self.meta, public=public
            )
        return hub.Dataset(destination, token=token, fs=fs, public=public)

    def resize_shape(self, size: int) -> None:
        """ Resize the shape of the dataset by resizing each tensor first dimension """
        if size == self._shape[0]:
            return
        self._shape = (int(size),)
        self.indexes = list(range(self.shape[0]))
        self.meta = self._store_meta()
        for t in self._tensors.values():
            t.resize_shape(int(size))

        self._update_dataset_state()

    def append_shape(self, size: int):
        """ Append the shape: Heavy Operation """
        size += self._shape[0]
        self.resize_shape(size)

    def rename(self, name: str) -> None:
        """ Renames the dataset """
        self._name = name
        self.meta = self._store_meta()
        self.flush()

    def delete(self):
        """ Deletes the dataset """
        fs, path = self._fs, self._path
        exist_meta = fs.exists(posixpath.join(path, "meta.json"))
        if exist_meta:
            fs.rm(path, recursive=True)
            if self.username is not None:
                HubControlClient().delete_dataset_entry(
                    self.username, self.dataset_name
                )
            return True
        return False

    def to_pytorch(
        self,
        transform=None,
        inplace=True,
        output_type=dict,
        indexes=None,
    ):
        """| Converts the dataset into a pytorch compatible format.

        Parameters
        ----------
        transform: function that transforms data in a dict format
        inplace: bool, optional
            Defines if data should be converted to torch.Tensor before or after Transforms applied (depends on what data
            type you need for Transforms). Default is True.
        output_type: one of list, tuple, dict, optional
            Defines the output type. Default is dict - same as in original Hub Dataset.
        indexes: list or int, optional
            The samples to be converted into tensorflow format. Takes all samples in dataset by default.
        """
        from .integrations import _to_pytorch

        ds = _to_pytorch(self, transform, inplace, output_type, indexes)
        return ds

    def to_tensorflow(self, indexes=None, include_shapes=False):
        """| Converts the dataset into a tensorflow compatible format

        Parameters
        ----------
        indexes: list or int, optional
            The samples to be converted into tensorflow format. Takes all samples in dataset by default.
        include_shapes: boolean, optional
            False by default. Setting it to True passes the shapes to tf.data.Dataset.from_generator.
            Setting to True could lead to issues with dictionaries inside Tensors.
        """
        from .integrations import _to_tensorflow

        ds = _to_tensorflow(self, indexes, include_shapes)
        return ds

    def _get_dictionary(self, subpath, slice_=None):
        """Gets dictionary from dataset given incomplete subpath"""
        tensor_dict = {}
        subpath = subpath if subpath.endswith("/") else subpath + "/"
        for key in self.keys:
            if key.startswith(subpath):
                suffix_key = key[len(subpath) :]
                split_key = suffix_key.split("/")
                cur = tensor_dict
                for i in range(len(split_key) - 1):
                    if split_key[i] not in cur.keys():
                        cur[split_key[i]] = {}
                    cur = cur[split_key[i]]
                slice_ = slice_ or slice(0, self._shape[0])
                tensorview = TensorView(
                    dataset=self, subpath=key, slice_=slice_, lazy=self.lazy
                )
                cur[split_key[-1]] = tensorview if self.lazy else tensorview.compute()
        if not tensor_dict:
            raise KeyError(f"Key {subpath} was not found in dataset")
        return tensor_dict

    def __iter__(self):
        """ Returns Iterable over samples """
        for i in range(len(self)):
            yield self[i]

    def __len__(self):
        """ Number of samples in the dataset """
        return self._shape[0]

    def disable_lazy(self):
        self.lazy = False

    def enable_lazy(self):
        self.lazy = True

    def _save_meta(self):
        _meta = json.loads(self._fs_map["meta.json"])
        _meta["meta_info"] = self._meta_information
        self._fs_map["meta.json"] = json.dumps(_meta).encode("utf-8")

    def flush(self):
        """Save changes from cache to dataset final storage.
        Does not invalidate this object.
        """
        if "r" in self._mode:
            return
        for t in self._tensors.values():
            t.flush()
        self._save_meta()
        self._fs_map.flush()
        self._update_dataset_state()

    def commit(self):
        """ Deprecated alias to flush()"""
        logger.warning("commit() is deprecated. Use flush() instead")
        self.flush()

    def close(self):
        """Save changes from cache to dataset final storage.
        This invalidates this object.
        """
        self.flush()
        for t in self._tensors.values():
            t.close()
        self._fs_map.close()
        self._update_dataset_state()

    def _update_dataset_state(self):
        if self.username is not None:
            HubControlClient().update_dataset_state(
                self.username, self.dataset_name, "UPLOADED"
            )

    def numpy(self, label_name=False):
        """Gets the values from different tensorview objects in the dataset schema

        Parameters
        ----------
        label_name: bool, optional
            If the TensorView object is of the ClassLabel type, setting this to True would retrieve the label names
            instead of the label encoded integers, otherwise this parameter is ignored.
        """
        return np.array(
            [
                create_numpy_dict(self, i, label_name=label_name)
                for i in range(self._shape[0])
            ]
        )

    def compute(self, label_name=False):
        """Gets the values from different tensorview objects in the dataset schema

        Parameters
        ----------
        label_name: bool, optional
            If the TensorView object is of the ClassLabel type, setting this to True would retrieve the label names
            instead of the label encoded integers, otherwise this parameter is ignored.
        """
        return self.numpy(label_name=label_name)

    def __str__(self):
        return (
            "Dataset(schema="
            + str(self._schema)
            + ", url="
            + "'"
            + self._url
            + "'"
            + ", shape="
            + str(self._shape)
            + ", mode="
            + "'"
            + self._mode
            + "')"
        )

    def __repr__(self):
        return self.__str__()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, exc_traceback):
        self.close()

    @property
    def keys(self):
        """
        Get Keys of the dataset
        """
        return self._tensors.keys()

    def _get_mode(self, mode: str, fs: AbstractFileSystem):
        if mode:
            if mode not in ["r", "r+", "a", "a+", "w", "w+"]:
                raise Exception(f"Invalid mode {mode}")
            return mode
        else:
            try:
                meta_path = posixpath.join(self._path, "meta.json")
                if not fs.exists(self._path) or not fs.exists(meta_path):
                    return "a"
                bytes_ = bytes("Hello", "utf-8")
                path = posixpath.join(self._path, "mode_test")
                fs.pipe(path, bytes_)
                fs.rm(path)
            except BaseException:
                return "r"
            return "a"

    @staticmethod
    def from_tensorflow(ds, scheduler: str = "single", workers: int = 1):
        """Converts a tensorflow dataset into hub format.

        Parameters
        ----------
        dataset:
            The tensorflow dataset object that needs to be converted into hub format
        scheduler: str
            choice between "single", "threaded", "processed"
        workers: int
            how many threads or processes to use

        Examples
        --------
        >>> ds = tf.data.Dataset.from_tensor_slices(tf.range(10))
        >>> out_ds = hub.Dataset.from_tensorflow(ds)
        >>> res_ds = out_ds.store("username/new_dataset") # res_ds is now a usable hub dataset

        >>> ds = tf.data.Dataset.from_tensor_slices({'a': [1, 2], 'b': [5, 6]})
        >>> out_ds = hub.Dataset.from_tensorflow(ds)
        >>> res_ds = out_ds.store("username/new_dataset") # res_ds is now a usable hub dataset

        >>> ds = hub.Dataset(schema=my_schema, shape=(1000,), url="username/dataset_name", mode="w")
        >>> ds = ds.to_tensorflow()
        >>> out_ds = hub.Dataset.from_tensorflow(ds)
        >>> res_ds = out_ds.store("username/new_dataset") # res_ds is now a usable hub dataset
        """
        from .integrations import _from_tensorflow

        ds = _from_tensorflow(ds, scheduler, workers)
        return ds

    @staticmethod
    def from_tfds(
        dataset,
        split=None,
        num: int = -1,
        sampling_amount: int = 1,
        scheduler: str = "single",
        workers: int = 1,
    ):
        """| Converts a TFDS Dataset into hub format.

        Parameters
        ----------
        dataset: str
            The name of the tfds dataset that needs to be converted into hub format
        split: str, optional
            A string representing the splits of the dataset that are required such as "train" or "test+train"
            If not present, all the splits of the dataset are used.
        num: int, optional
            The number of samples required. If not present, all the samples are taken.
            If count is -1, or if count is greater than the size of this dataset, the new dataset will contain all elements of this dataset.
        sampling_amount: float, optional
            a value from 0 to 1, that specifies how much of the dataset would be sampled to determinte feature shapes
            value of 0 would mean no sampling and 1 would imply that entire dataset would be sampled
        scheduler: str
            choice between "single", "threaded", "processed"
        workers: int
            how many threads or processes to use

        Examples
        --------
        >>> out_ds = hub.Dataset.from_tfds('mnist', split='test+train', num=1000)
        >>> res_ds = out_ds.store("username/mnist") # res_ds is now a usable hub dataset
        """
        from .integrations import _from_tfds

        ds = _from_tfds(dataset, split, num, sampling_amount, scheduler, workers)
        return ds

    @staticmethod
    def from_pytorch(dataset, scheduler: str = "single", workers: int = 1):
        """| Converts a pytorch dataset object into hub format

        Parameters
        ----------
        dataset:
            The pytorch dataset object that needs to be converted into hub format
        scheduler: str
            choice between "single", "threaded", "processed"
        workers: int
            how many threads or processes to use
        """

        from .integrations import _from_pytorch

        ds = _from_pytorch(dataset, scheduler, workers)
        return ds

    @staticmethod
    def from_directory(
        path_to_dir,
        labels=None,
        dtype="uint8",
        scheduler: str = "single",
        workers: int = 1,
    ):
        """|  This utility function is specific to create dataset from the categorical image dataset to easy use for the categorical image usecase.

        Parameters
        --------
            path_to_dir:str
            path of the directory where the image dataset root folder exists.

            labels:list
            passed a list of class names

            dtype:str
            datatype of the images can be defined by user.Default uint8.

            scheduler: str
            choice between "single", "threaded", "processed"

            workers: int
            how many threads or processes to use


        ---------
        Returns A dataset object for user use and to store a defined path.

        >>>ds = Dataset.from_directory('path/test')
        >>>ds.store('store_here')
        """

        def get_max_shape(path_to_dir):
            """| get_max_shape from  the images.

            -------
            path_to_dir:str path to the root directory

            -------
            return the maximum shape of the image

            -------

            """
            try:

                for i in os.listdir(path_to_dir):
                    for j in os.listdir(os.path.join(path_to_dir, i)):

                        if j.endswith((".png", ".jpg", ".jpeg", ".tiff", ".bmp")):
                            img_path = os.path.join(path_to_dir, i, j)
                        else:
                            print(
                                f"{os.path.join(path_to_dir,i,j)} is a non image file please remove it to execute...."
                            )

                        image = im.open(img_path)

                        width = set()
                        height = set()
                        mode = set()

                        detail = list(image.size)

                        width.add(detail[0])
                        height.add(detail[1])

                        if image.mode == "RGB":
                            mode.add(3)
                        elif image.mode == "RGBA":
                            mode.add(4)
                        elif image.mode == "LA":
                            mode.add(2)
                        else:
                            mode.add(1)

                max_shape = [max(width), max(height), max(mode)]
                return max_shape
            except Exception:
                print("check your data for fix")

        def make_schema(path_to_dir, labels, dtype):
            """| make_schema internal function to generate the schema internally."""
            max_shape = get_max_shape(path_to_dir)
            image_shape = (None, None, None)
            if labels is None:
                labels = ClassLabel(names=os.listdir(path_to_dir))
            else:
                labels = ClassLabel(labels)
            schema = {
                "label": labels,
                "image": Tensor(
                    shape=image_shape,
                    max_shape=max_shape,
                    dtype=dtype,
                ),
            }

            return schema

        schema = make_schema(path_to_dir, labels, dtype)

        if labels is not None:

            label_dic = {}
            for i, label in enumerate(labels):
                label_dic[label] = i
        else:
            labels_v = os.listdir(path_to_dir)
            label_dic = {}
            for i, label in enumerate(labels_v):
                label_dic[label] = i

        @hub.transform(schema=schema, scheduler=scheduler, workers=workers)
        def upload_data(sample):
            """| This upload_data function is for upload the images internally using `hub.transform`."""

            path_to_image = sample[1]

            pre_image = im.open(path_to_image)
            image = np.asarray(pre_image)
            image = image.astype(sample[2])
            image_shape = get_max_shape(path_to_dir)

            if pre_image.mode == "RGB":
                image = np.resize(image, (*image_shape[:2], 3))
            elif pre_image.mode == "RGBA":
                image = np.resize(image, (*image_shape[:2], 4))
            elif pre_image.mode == "LA":
                image = np.resize(image, (*image_shape[:2], 2))
            else:
                image = np.resize(image, (*image_shape[:2], 1))

            return {"label": label_dic[sample[0]], "image": image}

        images = []
        labels_list = []
        dataype = []
        for i in os.listdir(path_to_dir):
            for j in os.listdir(os.path.join(path_to_dir, i)):
                image = os.path.join(path_to_dir, i, j)
                images.append(image)
                labels_list.append(i)
                dataype.append(dtype)

        ds = upload_data(zip(labels_list, images, dataype))
        return ds
