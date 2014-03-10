"""
Dataset preloading tool

This file provides the ability to make a local cache of a dataset or part of
it. It is meant to help in the case where multiple jobs are reading the same
dataset from ${PYLEARN2_DATA_PATH}, which may cause a great burden on the
network.

With this file, it is possible to make a local copy
(in ${PYLEARN2_LOCAL_DATA_PATH}) of any required file and have multiple
processes use it simultaneously instead of all acquiring their own copy
over the network.

Workflow:


workflow:
1. Check if ${PYLEARN2_LOCAL_DATA_PATH} exists, if not, create it.
   This step needs a lock under the dir '/tmp/'
2. Check if the required dataset D exists under ${PYLEARN2_LOCAL_DATA_PATH}.
   If yes, load it. END
   Otherwise:
       Check if the local node has enough space to maintain a local copy of D.
          If no, read D directly from remote ${PYLEARN2_DATA_PATH}. END
          If yes:
            Get a lock under ${PYLEARN2_LOCAL_DATA_PATH}
            Check if D has been copied there by some other jobs.
               If yes, load it, release the lock. END
               if no,
                  Check if the local node has enough space to maintain
                  a local copy of D.  If no, read D directly from
                  ${PYLEARN2_DATA_PATH}. release lock. END If yes,
                  copy D from remote server, read it locally. release
                  lock. END

Whenever a folder or a dataset copy is created locally, it is granted
the same access as it has under ${PYLEARN2_LOCAL_DATA_PATH}. This is
gauranteed by default copy.

"""

import os
import time
import atexit
from pylearn2.utils import string_utils
import theano.gof.compilelock as compilelock


class DatasetCache:

    def __init__(self, verbose=False):
        default_path = '${PYLEARN2_DATA_PATH}'
        self.dataset_remote_dir = string_utils.preprocess(default_path)
        self.pid = os.getpid()
        self.verbose = verbose

        try:
            local_path = '${PYLEARN2_LOCAL_DATA_PATH}'
            self.dataset_local_dir = string_utils.preprocess(local_path)
        except:
            # Local cache seems to be deactivated
            self.dataset_local_dir = ""

    def load_dataset(self, remote_name):

        # Check if a local directory for data has been defined. Otherwise,
        # do not locally copy the data
        if self.dataset_local_dir == "":
            self._write("Local cache deactivated : file %s not cached" %
                       remote_name)
            return remote_name

        # Make sure the file to cache exists and really is a file
        if not os.path.exists(remote_name):
            self._write("Error : Specified file %s does not exist" %
                       remote_name)
            return remote_name

        if not os.path.isfile(remote_name):
            self._write("Error : Specified name %s is not a file" %
                       remote_name)
            return remote_name

        # Create the $PYLEARN2_LOCAL_DATA_PATH folder if needed
        self.safe_mkdir(self.dataset_local_dir)

        # Determine local path to which the file is to be cached
        local_name = os.path.join(self.dataset_local_dir,
                                  os.path.relpath(remote_name,
                                                  self.dataset_remote_dir))

        # Ensure there is enough space locally to cache the file
        if not self.check_enough_space(remote_name, local_name):
            self._write("Not enough free space : file %s not cached" %
                       remote_name)
            return remote_name

        # Create the folder structure to receive the remote file
        local_folder = os.path.split(local_name)[0]
        self.safe_mkdir(local_folder)

        # Acquire writelock on the local file to prevent the possibility
        # of any other process modifying it while we cache it if needed.
        self.getWriteLock(local_name)

        # If the file does not exist locally, consider creating it
        if not os.path.exists(local_name):

            # Check again that there is enough space to cache the file
            if not self.check_enough_space(remote_name, local_name):
                self._write("Not enough free space : file %s not cached" %
                           remote_name)
                self.releaseWriteLock()
                return remote_name

            # There is enough space; make a local copy of the file
            self.copy_from_server_to_local(remote_name, local_name)
            self._write("File %s has been locally cached to %s" %
                       (remote_name, local_name))

        else:
            self._write("File %s has previously been locally cached to %s" %
                       (remote_name, local_name))

        # Obtain a readlock on the downloaded file before releasing the
        # write lock
        self.getReadLock(local_name)
        self.releaseWriteLock()

        return local_name

    def _write(self, message):
        """
        Print message to the console if verbose
        """
        
        if self.verbose:
            print message

    def copy_from_server_to_local(self, remote_fname, local_fname):
        """
        Copies a remote file locally
        """

        head, tail = os.path.split(local_fname)
        head += os.path.sep
        if not os.path.exists(head):
            os.makedirs(os.path.dirname(head))

        command = 'cp ' + remote_fname + ' ' + local_fname
        os.system(command)

    def disk_usage(self, path):
        """
        Return free usage about the given path, in bytes
        """

        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        return total, used

    def check_enough_space(self, remote_fname, local_fname):
        """
        Check if the local disk has enough space to store the dataset
        """

        storage_need = os.path.getsize(remote_fname)
        storage_total, storage_used = self.disk_usage(self.dataset_local_dir)

        # Instead of only looking if there's enough space, we ensure we do not
        # go over 90% usage level to avoid filling the disk/partition
        return (storage_used + storage_need) < (storage_total * 0.90)

    def safe_mkdir(self, folderName):
        """
        Create the specified folder. If the parent folders do not
        exist, they are also created. If the folder already exists,
        nothing is done.
        """

        intermediaryFolders = folderName.split("/")

        # Remove invalid elements from intermediaryFolders
        if intermediaryFolders[-1] == "":
            intermediaryFolders = intermediaryFolders[:-1]

        for i in range(len(intermediaryFolders)):
            folderToCreate = "/".join(intermediaryFolders[:i+1]) + "/"

            if not os.path.exists(folderToCreate):
                try:
                    os.mkdir(folderToCreate)
                except:
                    pass

    def getReadLock(self, path):
        """
        Obtain a readlock on a file
        """

        timestamp = int(time.time() * 1e6)
        lockdirName = "%s.readlock.%i.%i" % (path, self.pid, timestamp)
        os.mkdir(lockdirName)

        # Register function to release the readlock at the end of the script
        atexit.register(self.releaseReadLock, lockdirName=lockdirName)

    def releaseReadLock(self, lockdirName):
        """
        Release a previously obtained readlock
        """

        # Make sure the lock still exists before deleting it
        if (os.path.exists(lockdirName) and os.path.isdir(lockdirName)):
            os.rmdir(lockdirName)

    def getWriteLock(self, filename):
        """
        Obtain a writelock on a file.
        Only one write lock may be held at any given time.
        """

        # compilelock expect locks to be on folder. Since we want a lock on a
        # file, we will have to ask compilelock for a folder with a different
        # name from the file we want a lock on or else compilelock will
        # try to create a folder with the same name as the file
        compilelock.get_lock(filename + ".writelock")

    def releaseWriteLock(self):
        """
        Release the previously obtained writelock
        """
        compilelock.release_lock()
