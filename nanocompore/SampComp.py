# -*- coding: utf-8 -*-

#~~~~~~~~~~~~~~IMPORTS~~~~~~~~~~~~~~#
# Std lib
from collections import *
import shelve
import multiprocessing as mp
import traceback
import datetime
import os

# Third party
from loguru import logger
import yaml
from tqdm import tqdm
import numpy as np
from pyfaidx import Fasta

# Local package
from nanocompore.common import *
from nanocompore.Whitelist import Whitelist
from nanocompore.TxComp import txCompare
from nanocompore.SampCompDB import SampCompDB
import nanocompore as pkg

# Disable multithreading for MKL and openBlas
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["MKL_THREADING_LAYER"] = "sequential"
os.environ["NUMEXPR_NUM_THREADS"] = "1"
os.environ["OMP_NUM_THREADS"] = "1"
os.environ['OPENBLAS_NUM_THREADS'] = '1'

#~~~~~~~~~~~~~~MAIN CLASS~~~~~~~~~~~~~~#
class SampComp(object):
    """ Init analysis and check args"""

    #~~~~~~~~~~~~~~FUNDAMENTAL METHODS~~~~~~~~~~~~~~#

    def __init__(self,
        eventalign_fn_dict:dict,
        fasta_fn:str,
        bed_fn:str = None,
        outpath:str = "results",
        outprefix:str = "out_",
        overwrite:bool = False,
        whitelist:Whitelist = None,
        comparison_methods:list = ["GMM", "KS"],
        logit:bool = False,
        allow_warnings:bool = False,
        sequence_context:int = 0,
        sequence_context_weights:str = "uniform",
        min_coverage:int = 30,
        min_ref_length:int = 100,
        downsample_high_coverage:int = 0,
        max_invalid_kmers_freq:float = 0.1,
        select_ref_id:list = [],
        exclude_ref_id:list = [],
        nthreads:int = 3,
        progress:bool = False):

        """
        Initialise a `SampComp` object and generates a white list of references with sufficient coverage for subsequent analysis.
        The retuned object can then be called to start the analysis.
        * eventalign_fn_dict
            Multilevel dictionnary indicating the condition_label, sample_label and file name of the eventalign_collapse output.
            2 conditions are expected and at least 2 sample replicates per condition are highly recommended.
            One can also pass YAML file describing the samples instead.
            Example `d = {"S1": {"R1":"path1.tsv", "R2":"path2.tsv"}, "S2": {"R1":"path3.tsv", "R2":"path4.tsv"}}`
        * outpath
            Path to the output folder.
        * outprefix
            text outprefix for all the files generated by the function.
        * overwrite
            If the output directory already exists, the standard behaviour is to raise an error to prevent overwriting existing data
            This option ignore the error and overwrite data if they have the same outpath and outprefix.
        * fasta_fn
            Path to a fasta file corresponding to the reference used for read alignment.
        * bed_fn
            Path to a BED file containing the annotation of the transcriptome used as reference when mapping.
        * whitelist
            Whitelist object previously generated with nanocompore Whitelist. If not given, will be automatically generated.
        * comparison_methods
            Statistical method to compare the 2 samples (mann_whitney or MW, kolmogorov_smirnov or KS, t_test or TT, gaussian_mixture_model or GMM).
            This can be a list or a comma separated string. {MW,KS,TT,GMM}
        * logit
            Force logistic regression even if we have less than 2 replicates in any condition.
        * allow_warnings
            If True runtime warnings during the ANOVA tests don't raise an error.
        * sequence_context
            Extend statistical analysis to contigous adjacent base if available.
        * sequence_context_weights
            type of weights to used for combining p-values. {uniform,harmonic}
        * min_coverage
            minimal read coverage required in all sample.
        * min_ref_length
            minimal length of a reference transcript to be considered in the analysis
        * downsample_high_coverage
            For reference with higher coverage, downsample by randomly selecting reads.
        * max_invalid_kmers_freq
            maximum frequency of NNNNN, mismatching and missing kmers in reads.
        * select_ref_id
            if given, only reference ids in the list will be selected for the analysis.
        * exclude_ref_id
            if given, refid in the list will be excluded from the analysis.
        * nthreads
            Number of threads (two are used for reading and writing, all the others for parallel processing).
        * progress
            Display a progress bar during execution
        """
        logger.info("Checking and initialising SampComp")

        # Save init options in dict for later
        log_init_state(loc=locals())

        # If eventalign_fn_dict is not a dict try to load a YAML file instead
        if type(eventalign_fn_dict) == str:
            logger.debug("Parsing YAML file")
            if not access_file(eventalign_fn_dict):
                raise NanocomporeError("{} is not a valid file".format(eventalign_fn_dict))
            with open(eventalign_fn_dict, "r") as fp:
                eventalign_fn_dict = yaml.load(fp, Loader=yaml.SafeLoader)

        # Check eventalign_dict file paths and labels
        eventalign_fn_dict = self.__check_eventalign_fn_dict(eventalign_fn_dict)
        logger.debug(eventalign_fn_dict)

        # Check if fasta and bed files exist
        if not access_file(fasta_fn):
            raise NanocomporeError("{} is not a valid FASTA file".format(fasta_fn))
        if bed_fn and not access_file(bed_fn):
            raise NanocomporeError("{} is not a valid BED file".format(bed_fn))

        # Check at least 3 threads
        if nthreads < 3:
            raise NanocomporeError("The minimum number of threads is 3")

        # Parse comparison methods
        if comparison_methods:
            if type(comparison_methods) == str:
                comparison_methods = comparison_methods.split(",")
            for i, method in enumerate(comparison_methods):
                method = method.upper()
                if method in ["MANN_WHITNEY", "MW"]:
                    comparison_methods[i]="MW"
                elif method in ["KOLMOGOROV_SMIRNOV", "KS"]:
                    comparison_methods[i]="KS"
                elif method in ["T_TEST", "TT"]:
                    comparison_methods[i]="TT"
                elif method in ["GAUSSIAN_MIXTURE_MODEL", "GMM"]:
                    comparison_methods[i]="GMM"
                else:
                    raise NanocomporeError("Invalid comparison method {}".format(method))

        if not whitelist:
            whitelist = Whitelist(
                eventalign_fn_dict = eventalign_fn_dict,
                fasta_fn = fasta_fn,
                min_coverage = min_coverage,
                min_ref_length = min_ref_length,
                downsample_high_coverage = downsample_high_coverage,
                max_invalid_kmers_freq = max_invalid_kmers_freq,
                select_ref_id = select_ref_id,
                exclude_ref_id = exclude_ref_id)
        elif not isinstance(whitelist, Whitelist):
            raise NanocomporeError("Whitelist is not valid")

        # Set private args from whitelist args
        self.__min_coverage = whitelist._Whitelist__min_coverage
        self.__downsample_high_coverage = whitelist._Whitelist__downsample_high_coverage
        self.__max_invalid_kmers_freq = whitelist._Whitelist__max_invalid_kmers_freq

        # Save private args
        self.__eventalign_fn_dict = eventalign_fn_dict
        self.__db_fn = os.path.join(outpath, outprefix+"SampComp.db")
        self.__fasta_fn = fasta_fn
        self.__bed_fn = bed_fn
        self.__whitelist = whitelist
        self.__comparison_methods = comparison_methods
        self.__logit = logit
        self.__allow_warnings = allow_warnings
        self.__sequence_context = sequence_context
        self.__sequence_context_weights = sequence_context_weights
        self.__nthreads = nthreads - 2
        self.__progress = progress

        # Get number of samples
        n = 0
        for sample_dict in self.__eventalign_fn_dict.values():
            for sample_lab in sample_dict.keys():
                n+=1
        self.__n_samples = n

    def __call__(self):
        """
        Run the analysis
        """
        logger.info("Starting data processing")
        # Init Multiprocessing variables
        in_q = mp.Queue(maxsize = 100)
        out_q = mp.Queue(maxsize = 100)
        error_q = mp.Queue()

        # Define processes
        ps_list = []
        ps_list.append(mp.Process(target=self.__list_refid, args=(in_q, error_q)))
        for i in range(self.__nthreads):
            ps_list.append(mp.Process(target=self.__process_references, args=(in_q, out_q, error_q)))
        ps_list.append(mp.Process(target=self.__write_output, args=(out_q, error_q)))

        # Start processes and monitor error queue
        try:
            # Start all processes
            for ps in ps_list:
                ps.start()
            # Monitor error queue
            for tb in iter(error_q.get, None):
                logger.trace("Error caught from error_q")
                raise NanocomporeError(tb)

        # Catch error and reraise it
        except(BrokenPipeError, KeyboardInterrupt, NanocomporeError) as E:
            logger.error("An error occured. Killing all processes\n")
            raise E

        finally:
            # Soft processes stopping
            for ps in ps_list:
                ps.join()

            # Hard failsafe processes killing
            for ps in ps_list:
                if ps.exitcode == None:
                    ps.terminate()

        # Return database wrapper object
        return SampCompDB(
            db_fn=self.__db_fn,
            fasta_fn=self.__fasta_fn,
            bed_fn=self.__bed_fn)

    #~~~~~~~~~~~~~~PRIVATE MULTIPROCESSING METHOD~~~~~~~~~~~~~~#
    def __list_refid(self, in_q, error_q):
        """Add valid refid from whitelist to input queue to dispatch the data among the workers"""
        try:
            for ref_id, ref_dict in self.__whitelist:
                logger.debug("Adding {} to in_q".format(ref_id))
                in_q.put((ref_id, ref_dict))

            # Deal 1 poison pill and close file pointer
            logger.debug("Adding poison pill to in_q")
            for i in range(self.__nthreads):
                in_q.put(None)

        # Manage exceptions and deal poison pills
        except Exception:
            logger.debug("Error in Reader. Kill input queue")
            for i in range(self.__nthreads):
                in_q.put(None)
            error_q.put(traceback.format_exc())

    def __process_references(self, in_q, out_q, error_q):
        """
        Consume ref_id, agregate intensity and dwell time at position level and
        perform statistical analyses to find significantly different regions
        """
        try:
            logger.debug("Worker thread started")
            # Open all files for reading. File pointer are stored in a dict matching the ref_dict entries
            fp_dict = self.__eventalign_fn_open()

            # Process refid in input queue
            for ref_id, ref_dict in iter(in_q.get, None):
                logger.debug("Worker thread processing new item from in_q: {}".format(ref_id))
                # Create an empty dict for all positions first
                ref_pos_list = self.__make_ref_pos_list(ref_id)

                for cond_lab, sample_dict in ref_dict.items():
                    for sample_lab, read_list in sample_dict.items():
                        fp = fp_dict[cond_lab][sample_lab]

                        for read in read_list:

                            # Move to read, save read data chunk and reset file pointer
                            fp.seek(read["byte_offset"])
                            line_list = fp.read(read["byte_len"]).split("\n")
                            fp.seek(0)

                            # Check read_id ref_id concordance between index and data file
                            header = numeric_cast_list(line_list[0][1:].split("\t"))
                            if not header[0] == read["read_id"] or not header[1] == read["ref_id"]:
                                raise NanocomporeError("Index and data files are not matching:\n{}\n{}".format(header, read))

                            # Extract col names from second line
                            col_names = line_list[1].split("\t")
                            # Check that all required fields are present
                            if not all_values_in (["ref_pos", "ref_kmer", "median", "dwell_time"], col_names):
                                raise NanocomporeError("Required fields not found in the data file: {}".format(col_names))
                            # Verify if kmers events stats values are present or not
                            kmers_stats = all_values_in (["NNNNN_dwell_time", "mismatch_dwell_time"], col_names)

                            # Parse data files kmers per kmers
                            prev_pos = None
                            for line in line_list[2:]:
                                # Transform line to dict and cast str numbers to actual numbers
                                kmer = numeric_cast_dict(keys=col_names, values=line.split("\t"))
                                pos = kmer["ref_pos"]

                                # Check consistance between eventalign data and reference sequence
                                if kmer["ref_kmer"] != ref_pos_list[pos]["ref_kmer"]:
                                    ref_pos_list[pos]["ref_kmer"] = ref_pos_list[pos]["ref_kmer"]+"!!!!"
                                    #raise NanocomporeError ("Data reference kmer({}) doesn't correspond to the reference sequence ({})".format(ref_pos_list[pos]["ref_kmer"], kmer["ref_kmer"]))

                                # Fill dict with the current pos values
                                ref_pos_list[pos]["data"][cond_lab][sample_lab]["intensity"].append(kmer["median"])
                                ref_pos_list[pos]["data"][cond_lab][sample_lab]["dwell"].append(kmer["dwell_time"])
                                ref_pos_list[pos]["data"][cond_lab][sample_lab]["coverage"] += 1

                                if kmers_stats:
                                    # Fill in the missing positions
                                    if prev_pos and pos-prev_pos > 1:
                                        for missing_pos in range(prev_pos+1, pos):
                                            ref_pos_list[missing_pos]["data"][cond_lab][sample_lab]["kmers_stats"]["missing"] += 1
                                    # Also fill in with normalised position event stats
                                    n_valid = (kmer["dwell_time"]-(kmer["NNNNN_dwell_time"]+kmer["mismatch_dwell_time"])) / kmer["dwell_time"]
                                    n_NNNNN = kmer["NNNNN_dwell_time"] / kmer["dwell_time"]
                                    n_mismatching = kmer["mismatch_dwell_time"] / kmer["dwell_time"]
                                    ref_pos_list[pos]["data"][cond_lab][sample_lab]["kmers_stats"]["valid"] += n_valid
                                    ref_pos_list[pos]["data"][cond_lab][sample_lab]["kmers_stats"]["NNNNN"] += n_NNNNN
                                    ref_pos_list[pos]["data"][cond_lab][sample_lab]["kmers_stats"]["mismatching"] += n_mismatching
                                    # Save previous position
                                    prev_pos = pos

                logger.debug("Data for {} loaded.".format(ref_id))
                if self.__comparison_methods:
                    random_state=np.random.RandomState(seed=42)
                    ref_pos_list = txCompare(
                        ref_id=ref_id,
                        ref_pos_list=ref_pos_list,
                        methods=self.__comparison_methods,
                        sequence_context=self.__sequence_context,
                        sequence_context_weights=self.__sequence_context_weights,
                        min_coverage= self.__min_coverage,
                        allow_warnings=self.__allow_warnings,
                        logit=self.__logit,
                        random_state=random_state)

                # Add the current read details to queue
                logger.debug("Adding %s to out_q"%(ref_id))
                out_q.put((ref_id, ref_pos_list))

            # Deal 1 poison pill and close file pointer
            logger.debug("Adding poison pill to out_q")
            out_q.put(None)
            self.__eventalign_fn_close(fp_dict)

        # Manage exceptions, deal poison pills and close files
        except Exception as e:
            logger.error("Error in worker. Kill output queue")
            logger.error(e)
            for i in range(self.__nthreads):
                out_q.put(None)
            self.__eventalign_fn_close(fp_dict)
            error_q.put(traceback.format_exc())

    def __write_output(self, out_q, error_q):
        # Get results out of the out queue and write in shelve
        pvalue_tests = set()
        ref_id_list = []
        try:
            with shelve.open(self.__db_fn, flag='n') as db:
                # Iterate over the counter queue and process items until all poison pills are found
                pbar = tqdm(total = len(self.__whitelist), unit=" Processed References", disable=self.__log_level in ("warning", "debug"))
                for _ in range(self.__nthreads):
                    for ref_id, ref_pos_list in iter(out_q.get, None):
                        ref_id_list.append(ref_id)
                        logger.debug("Writer thread writing %s"%ref_id)
                        # Get pvalue fields available in analysed data before
                        for pos_dict in ref_pos_list:
                            if 'txComp' in pos_dict:
                                for res in pos_dict['txComp'].keys():
                                    if "pvalue" in res:
                                        pvalue_tests.add(res)
                        # Write results in a shelve db
                        db [ref_id] = ref_pos_list
                        pbar.update()

                # Write list of refid
                db["__ref_id_list"] = ref_id_list

                # Write metadata
                db["__metadata"] = {
                    "package_name": package_name,
                    "package_version": package_version,
                    "timestamp": str(datetime.datetime.now()),
                    "comparison_methods": self.__comparison_methods,
                    "pvalue_tests": sorted(list(pvalue_tests)),
                    "sequence_context": self.__sequence_context,
                    "min_coverage": self.__min_coverage,
                    "n_samples": self.__n_samples}

            # Ending process bar and deal poison pill in error queue
            pbar.close()
            error_q.put(None)

        # Manage exceptions and add error trackback to error queue
        except Exception:
            pbar.close()
            error_q.put(traceback.format_exc())

    #~~~~~~~~~~~~~~PRIVATE HELPER METHODS~~~~~~~~~~~~~~#
    def __check_eventalign_fn_dict(self, d):
        """"""
        # Check that the number of condition is 2 and raise a warning if there are less than 2 replicates per conditions
        if len(d) != 2:
            raise NanocomporeError("2 conditions are expected. Found {}".format(len(d)))
        for cond_lab, sample_dict in d.items():
            if len(sample_dict) == 1:
                logger.info("Only 1 replicate found for condition {}".format(cond_lab))
                logger.info("This is not recommended. The statistics will be calculated with the logit method")

        # Test if files are accessible and verify that there are no duplicated replicate labels
        duplicated_lab = False
        rep_lab_list = []
        rep_fn_list = []
        for cond_lab, sd in d.items():
            for rep_lab, fn in sd.items():
                if not access_file(fn):
                    raise NanocomporeError("Cannot access eventalign file: {}".format(fn))
                if fn in rep_fn_list:
                    raise NanocomporeError("Duplicated eventalign file detected: {}".format(fn))
                if rep_lab in rep_lab_list:
                    duplicated_lab = True
                rep_lab_list.append(rep_lab)
                rep_fn_list.append(fn)
        if not duplicated_lab:
            return d

        # If duplicated replicate labels found, prefix labels with condition name
        else:
            logger.debug("Found duplicated labels in the replicate names. Prefixing with condition name")
            d_clean = OrderedDict()
            for cond_lab, sd in d.items():
                d_clean[cond_lab] = OrderedDict()
                for rep_lab, fn in sd.items():
                    d_clean[cond_lab]["{}_{}".format(cond_lab, rep_lab)] = fn
            return d_clean

    def __eventalign_fn_open(self):
        """"""
        fp_dict = OrderedDict()
        for cond_lab, sample_dict in self.__eventalign_fn_dict.items():
            fp_dict[cond_lab] = OrderedDict()
            for sample_lab, fn in sample_dict.items():
                fp_dict[cond_lab][sample_lab] = open(fn, "r")
        return fp_dict

    def __eventalign_fn_close(self, fp_dict):
        """"""
        for sample_dict in fp_dict.values():
            for fp in sample_dict.values():
                fp.close()

    def __make_ref_pos_list(self, ref_id):
        """"""
        ref_pos_list = []
        with Fasta(self.__fasta_fn) as fasta:
            ref_fasta = fasta [ref_id]
            ref_len = len(ref_fasta)
            ref_seq = str(ref_fasta)

            for pos in range(ref_len-4):
                pos_dict = OrderedDict()
                pos_dict["ref_kmer"] = ref_seq[pos:pos+5]
                pos_dict["data"] = OrderedDict()
                for cond_lab, s_dict in self.__eventalign_fn_dict.items():
                    pos_dict["data"][cond_lab] = OrderedDict()
                    for sample_lab in s_dict.keys():

                        pos_dict["data"][cond_lab][sample_lab] = {
                            "intensity":[],
                            "dwell":[],
                            "coverage":0,
                            "kmers_stats":{"missing":0,"valid":0,"NNNNN":0,"mismatching":0}}
                ref_pos_list.append(pos_dict)
        return ref_pos_list
