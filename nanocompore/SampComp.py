# -*- coding: utf-8 -*-

#~~~~~~~~~~~~~~IMPORTS~~~~~~~~~~~~~~#
# Std lib
import logging
from collections import OrderedDict
import shelve
import multiprocessing as mp

# Third party
from tqdm import tqdm

# Local package
#from nanocompore.txCompare import txCompare
from nanocompore.helper_lib import mkdir, access_file, mytqdm
from nanocompore.Whitelist import Whitelist
from nanocompore.NanocomporeError import NanocomporeError

# Logger setup
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)
logLevel_dict = {"debug":logging.DEBUG, "info":logging.INFO, "warning":logging.WARNING}

#~~~~~~~~~~~~~~MAIN CLASS~~~~~~~~~~~~~~#
class SampComp (object):
    """ Produce useful results. => Thanks Tommaso ! That's a very *useful* comment :P """

    #~~~~~~~~~~~~~~MAGIC METHODS~~~~~~~~~~~~~~#

    def __init__(self,
        s1_fn,
        s2_fn,
        whitelist,
        output_db_fn,
        padj_threshold = 0.1,
        comparison_method = "kmean",
        sequence_context=0,
        nthreads = 4,
        logLevel = "info",):

        """
        Main routine that starts readers and consumers
            s1_fn: path to a nanopolish eventalign collapsed file corresponding to sample 1
            s2_fn: path to a nanopolish eventalign collapsed file corresponding to sample 2
            outfolder: path to folder where to save output
            nthreads: number of consumer threads
            whitelist_dict: Dictionnary generated by nanocopore whitelist function
        """
        # Set logging level
        logger.setLevel (logLevel_dict.get (logLevel, logging.WARNING))
        logger.info ("Initialise and checks options")

        # Check args
        if not isinstance (whitelist, Whitelist):
            raise NanocomporeError("Whitelist is not valid")
        for fn in (s1_fn, s2_fn):
            if not access_file (fn):
                raise NanocomporeError("Cannot access file {}".format(fp))
        if nthreads < 3:
            raise NanocomporeError("Number of threads not valid")

        # Save private args
        self.__s1_fn = s1_fn
        self.__s2_fn = s2_fn
        self.__whitelist = whitelist
        self.__output_db_fn = output_db_fn
        self.__padj_threshold = padj_threshold
        self.__comparison_method = comparison_method
        self.__sequence_context = sequence_context
        self.__nthreads = nthreads - 2 # Remove reader and writer threads
        self.__logLevel = logLevel

        logger.info ("Start data processing")
        # Init Multiprocessing variables
        in_q = mp.Queue (maxsize = 1000)
        out_q = mp.Queue (maxsize = 1000)

        # Define processes
        ps_list = []
        ps_list.append (mp.Process (target=self.__read_eventalign_files, args=(in_q,)))
        for i in range (self.__nthreads):
            ps_list.append (mp.Process (target=self.__process_references, args=(in_q, out_q)))
        ps_list.append (mp.Process (target=self.__write_output, args=(out_q,)))

        # Start processes and block until done
        try:
            for ps in ps_list:
                ps.start ()
            for ps in ps_list:
                ps.join ()

        # Kill processes if early stop
        except (BrokenPipeError, KeyboardInterrupt) as E:
            if self.verbose: stderr_print ("Early stop. Kill processes\n")
            for ps in ps_list:
                ps.terminate ()

    #~~~~~~~~~~~~~~PRIVATE MULTIPROCESSING METHOD~~~~~~~~~~~~~~#
    def __read_eventalign_files (self, in_q):
        # Add refid to inqueue to dispatch the data among the workers
        with open (self.__s1_fn) as s1_fp, open (self.__s2_fn) as s2_fp:
            for ref_id, ref_dict in self.__whitelist:

                # Init empty dict for all positions in valid intervals
                position_dict = OrderedDict ()
                for interval_start, interval_end in ref_dict["interval_list"]:
                    for i in range (interval_start, interval_end+1):
                        position_dict[i] = {"S1":[], "S2":[]}

                # Parse S1 and S2 reads data and add to mean and dwell time per position
                for lab, fp in (("S1", s1_fp), ("S2", s2_fp)):
                    for read in ref_dict[lab]:

                        # Move to read save read data chunk and reset file pointer
                        fp.seek (read.byte_offset)
                        read_lines = fp.read (read.byte_len)
                        fp.seek (0)

                        # Check if positions are in the ones found in the whitelist intervals
                        for line in read_lines.split("\n")[2:]:
                            ls = line.split("\t")
                            ref_pos = int(ls[0])

                            # Append mean value and dwell time per position
                            if ref_pos in position_dict:
                                position_dict[ref_pos][lab].append((float(ls[8]), int(ls[9])))

                in_q.put ((ref_id, position_dict))

        # Add 1 poison pill for each worker thread
        for i in range (self.__nthreads):
            in_q.put (None)

    def __process_references (self, in_q, out_q):
        # Consumme ref_id and position_dict until empty and perform statiscical analysis
        for ref_id, position_dict in iter (in_q.get, None):
            # Do stats with position_dicts
            ####### ## Add p-value per position to the position_dict #######
            ####### position_dict = tx_compare (self.__padj_threshold, self.__comparison_method, self.__sequence_context) #######
            # Add the current read details to queue
            out_q.put ((ref_id, position_dict))
        # Add poison pill in queues
        out_q.put (None)

    def __write_output (self, out_q): ############################################# Or pickle dict or flat file ...
        # Get results out of the out queue and write in shelve
        with shelve.open (self.__output_db_fn) as db:
            # Iterate over the counter queue and process items until all poison pills are found
            pbar = tqdm (total = len(self.__whitelist), unit=" Processed References", disable=self.__logLevel=="warning")
            for _ in range (self.__nthreads):
                for ref_id, stat_dict in iter (out_q.get, None):
                    # Write results in a shelve db to get around multithreaded isolation
                    db [ref_id] = stat_dict
                    pbar.update ()
            pbar.close()


    #### Write extra functions to write bed files and other kind of output
