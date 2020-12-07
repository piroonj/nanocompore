# -*- coding: utf-8 -*-

#~~~~~~~~~~~~~~IMPORTS~~~~~~~~~~~~~~#
# Std lib
from collections import *
import logging
from pkg_resources import resource_filename
import json
import datetime
import os

# Third party
import numpy as np
import pandas as pd
from pyfaidx import Fasta
import pyfaidx
from scipy.stats import logistic as sp_logistic
from scipy.stats import wald as sp_wald
import matplotlib.pyplot as pl
from tqdm import tqdm

# Local package
from nanocompore.common import *

#~~~~~~~~~~~~~~MAIN CLASS~~~~~~~~~~~~~~#
def SimReads(
    fasta_fn:str,
    outpath:str = "./",
    outprefix:str = "out",
    overwrite:bool = False,
    run_type:str= "RNA",
    ref_list:list = [],
    nreads_per_ref:int=100,
    plot:bool=False,
    intensity_mod:float = 0,
    dwell_mod:float = 0,
    mod_reads_freq:float = 0,
    mod_bases_freq:float = 0.25,
    mod_bases_type:str = "A",
    mod_extend_context:int = 2,
    min_mod_dist:int = 6,
    pos_rand_seed:int = 42,
    data_rand_seed:int = None,
    not_bound:bool = False,
    progress:bool = False):
    """
    Simulate reads in a NanopolishComp like file from a fasta file and an inbuild model.
    The simulated reads correspond to the sequences provided in the fasta file and follow
    the intensity and dwell time from the corresponding model (RNA or DNA).
    * fasta_fn
        Fasta file containing references to use to generate artificial reads.
    * outpath
        Path to the output folder.
    * outprefix
        text outprefix for all the files generated by the function.
    * overwrite
        If the output directory already exists, the standard behaviour is to raise an error to prevent overwriting existing data
        This option ignore the error and overwrite data if they have the same outpath and outprefix.
    * run_type
        Define the run type model to import {RNA,DNA}
    * ref_list
        Restrict the references to the listed IDs.
    * nreads_per_ref
        Number of reads to generate per references.
    * plot
        If true, generate an interactive plot of the trace generated.
    * intensity_mod
        Fraction of intensity distribution SD by which to modify the intensity distribution loc value.
    * dwell_mod
        Fraction of dwell time distribution SD by which to modify the intensity distribution loc value.
    * mod_reads_freq
        Frequency of reads to modify.
    * mod_bases_freq
        Frequency of bases to modify in each read (if possible).
    * mod_bases_type
        Base for which to modify the signal. {A,T,C,G}
    * mod_extend_context
        number of adjacent base affected by the signal modification following an harmonic series.
    * min_mod_dist
        Minimal distance between 2 bases to modify.
    * pos_rand_seed
        Define a seed for randon position picking to get a deterministic behaviour.
    * data_rand_seed
        Define a seed for generating the data. If None (default) the seed is drawn from /dev/urandom.
    * not_bound
        Do not bind the values generated by the distributions to the observed min and max observed values from the model file.
    * progress
        Display a progress bar during execution
    """
    logger.info("Checking and initialising Simreads")

    # Save init options in dict for later
    log_init_state(loc=locals())

    # Check if fasta file exists
    if not access_file(fasta_fn):
        raise NanocomporeError("{} is not a valid file".format(fasta_fn))

    # Define model depending on run_type
    if run_type == "RNA":
        logger.info("Importing RNA model file")
        model_fn = resource_filename("nanocompore", "models/kmers_model_RNA_r9.4_180mv.tsv")
        model_df = pd.read_csv(model_fn, sep="\t", comment="#", index_col=0)
    else:
        raise NanocomporeError("Only RNA is implemented at the moment")

    # Open fasta file and output files
    logger.info("Reading Fasta file and simulate corresponding data")

    with pyfaidx.Fasta(fasta_fn) as fasta_fp,\
         open(os.path.join(outpath, "{}.tsv".format(outprefix)) , "w") as data_fp,\
         open(os.path.join(outpath, "{}.tsv.idx".format(outprefix)), "w") as idx_fp,\
         open(os.path.join(outpath, "{}_pos.tsv".format(outprefix)), "w") as pos_fp:

        # Get all reference names if no ref_list
        if not ref_list:
            ref_list = fasta_fp.keys()

        # Write log and index file header and init
        idx_fp.write("ref_id\tread_id\tbyte_offset\tbyte_len\n")
        pos_fp.write("ref_id\tmodified_positions\n")

        byte_offset = 0
        # Simulate reference per reference
        for ref_num, ref_id in enumerate(tqdm((ref_list), unit=" References", disable=not progress))):
            logger.debug("Processing reference {}".format(ref_id))
            try:
                ref_seq = str(fasta_fp[ref_id])

                # Simulate data corresponding to the reference
                intensity_array, dwell_array, mod_pos_list, nreads_mod = simulate_ref_mod_context(
                    ref_seq = ref_seq,
                    model_df = model_df,
                    nreads = nreads_per_ref,
                    intensity_mod = intensity_mod,
                    dwell_mod = dwell_mod,
                    mod_reads_freq = mod_reads_freq,
                    mod_bases_freq = mod_bases_freq,
                    mod_bases_type = mod_bases_type,
                    mod_extend_context = mod_extend_context,
                    min_mod_dist = min_mod_dist,
                    pos_rand_seed=pos_rand_seed,
                    data_rand_seed=data_rand_seed,
                    not_bound=not_bound)

                # Plot traces if required
                if plot:
                    plot_trace(ref_id, intensity_array, dwell_array, mod_pos_list, nreads_mod)

                # Write options used in log file
                pos_fp.write("{}\t{}\n".format(ref_id, array_join(";", mod_pos_list)))

                # Write output in NanopolishComp like files
                for read_num in range(nreads_per_ref):
                    read_str = "#{}_{}\t{}\n".format(ref_num, read_num, ref_id)
                    read_str += "ref_pos\tref_kmer\tdwell_time\tmedian\n"
                    for ref_pos in range(len(ref_seq)-4):
                        read_str += "{}\t{}\t{}\t{}\n".format(
                            ref_pos,
                            ref_seq[ref_pos:ref_pos+5],
                            dwell_array [ref_pos, read_num],
                            intensity_array [ref_pos, read_num])

                    data_fp.write(read_str)
                    idx_fp.write("{}\t{}_{}\t{}\t{}\n".format(ref_id, ref_num, read_num, byte_offset, len(read_str)-1))
                    byte_offset += len(read_str)

            except KeyError:
                logger.debug("Reference {} not found in reference fasta file".format(ref_id))

def plot_trace(ref_id, intensity_array, dwell_array, mod_pos_list, nreads_mod):
    """"""
    with pl.style.context("ggplot"):
        fig, axes = pl.subplots(2, 1, figsize=(30,10))

        # Plot intensity data
        for i, line in enumerate(intensity_array.T):
            axes[0].plot(line, alpha=(1/len(intensity_array.T))*2, color="red" if i<nreads_mod else "black")
        axes[0].set_title("Median intensity")
        axes[0].set_xlim(0,len(intensity_array))

        # Plot dwell data
        for i, line in enumerate(dwell_array.T):
            axes[1].plot(line, alpha=(1/len(dwell_array.T))*2, color="red" if i<nreads_mod else "black")
        axes[1].set_title("Dwell time")
        axes[1].set_xlim(0,len(dwell_array))

        # Add lines where the signal is modified
        for x in mod_pos_list:
            axes[0].axvline(x, color="grey", linestyle=":")
            axes[1].axvline(x, color="grey", linestyle=":")

        # tweak plot
        fig.suptitle(ref_id, y=1.02, fontsize=18)
        fig.tight_layout()

def simulate_ref_mod_context(
    ref_seq,
    model_df,
    nreads=100,
    intensity_mod=0,
    dwell_mod=0,
    mod_reads_freq=0,
    mod_bases_freq=0,
    mod_bases_type="A",
    mod_extend_context=0,
    min_mod_dist=6,
    not_bound=False,
    pos_rand_seed=42,
    data_rand_seed=None):
    """"""

    # Create empty arrays to store reads intensity and dwell
    n_kmers = len(ref_seq)-4
    intensity_array = np.empty(shape=(n_kmers, nreads), dtype=np.float)
    dwell_array = np.empty(shape=(n_kmers, nreads), dtype=np.float)
    mod_pos_list = []
    nreads_mod = 0

    # Fill in arrays with non modified data per position
    for pos in range(n_kmers):
        kmer_seq =  ref_seq[pos:pos+5]
        kmer_model = model_df.loc[kmer_seq]

        # Sample intensity
        intensity_array[pos] = get_valid_distr_data(
            loc = kmer_model["model_intensity_loc"],
            scale = kmer_model["model_intensity_scale"],
            min = None if not_bound else kmer_model["raw_intensity_min"],
            max = None if not_bound else kmer_model["raw_intensity_max"],
            sp_distrib = sp_logistic,
            size=nreads,
            data_rand_seed=data_rand_seed)

        # Sample dwell
        dwell_array[pos] = get_valid_distr_data(
            loc = kmer_model["model_dwell_loc"],
            scale = kmer_model["model_dwell_scale"],
            min = None if not_bound else kmer_model["raw_dwell_min"],
            max = None if not_bound else kmer_model["raw_dwell_max"],
            sp_distrib = sp_wald,
            size=nreads,
            data_rand_seed=data_rand_seed)

    # If modifications are required, edit the values for randomly picked positions + adjacent positions if a context was given
    if mod_reads_freq and mod_bases_freq:
        # Define number of reads to modify and number not to modify
        nreads_mod = int(np.rint(nreads*mod_reads_freq))
        # Define positions to modify base on mod_base_freq and mod_base_type
        mod_pos_list = find_valid_pos_list(ref_seq, mod_bases_type, mod_bases_freq, min_mod_dist, pos_rand_seed)
        # if the modification context has to be extended
        mod_dict = make_mod_dict(intensity_mod, dwell_mod, mod_extend_context)


        for pos in mod_pos_list:
            for i in range(-mod_extend_context, mod_extend_context+1):
                pos_extend = pos+i
                if 0 <= pos_extend < n_kmers:
                    kmer_seq = ref_seq[pos_extend:pos_extend+5]
                    kmer_model = model_df.loc[kmer_seq]

                    if intensity_mod:
                        intensity_array[pos_extend][0:nreads_mod] = get_valid_distr_data(
                            loc = kmer_model["model_intensity_loc"],
                            scale = kmer_model["model_intensity_scale"],
                            min = None if not_bound else kmer_model["raw_intensity_min"],
                            max = None if not_bound else kmer_model["raw_intensity_max"],
                            mod = kmer_model["model_intensity_std"]*mod_dict["intensity"][i],
                            sp_distrib = sp_logistic,
                            size = nreads_mod,
                            data_rand_seed = data_rand_seed)

                    if dwell_mod:
                        dwell_array[pos_extend][0:nreads_mod] = get_valid_distr_data(
                            loc = kmer_model["model_dwell_loc"],
                            scale = kmer_model["model_dwell_scale"],
                            min = None if not_bound else kmer_model["raw_dwell_min"],
                            max = None if not_bound else kmer_model["raw_dwell_max"],
                            mod = kmer_model["model_dwell_std"]*mod_dict["dwell"][i],
                            sp_distrib = sp_wald,
                            size = nreads_mod,
                            data_rand_seed = data_rand_seed)

    return (intensity_array, dwell_array, mod_pos_list, nreads_mod)

def find_valid_pos_list(ref_seq, mod_bases_type, mod_bases_freq, min_mod_dist, pos_rand_seed=42):
    """"""
    pos_list = []
    for i in range(len(ref_seq)-4):
        if ref_seq[i] == mod_bases_type:
            pos_list.append(i)
    n_samples = int(np.rint(len(pos_list)*mod_bases_freq))
    logger.debug("\tTry to find {} kmers to modify".format(n_samples))

    i = 0
    while True:
        np.random.seed(pos_rand_seed*i)
        a = np.random.choice(pos_list, n_samples, replace=False)
        a.sort()
        if np.ediff1d(a).min() >= min_mod_dist :
            logger.debug("\tFound valid combination for {} kmers".format(n_samples))
            logger.debug("\tmodified positions: {}".format(a))
            break
        if i == 1000:
            i = 0
            n_samples -= 1
        i+=1
    return a

def get_valid_distr_data(loc, scale, size, sp_distrib, mod=None, min=None, max=None, max_tries=10000, data_rand_seed=None):
    """"""

    # If a mofidier is given modify the loc, min and max value accordingly
    if mod:
        loc+=mod
        if min:
            min+=mod
        if max:
            max+=mod

    # Define lower and upper bound if not given
    if not min:
        min=0
    if not max:
        max=np.finfo(np.float64).max

    # Try to sample the required number of data point
    i = 0
    while True:
        iter_rand_seed = None if data_rand_seed is None else data_rand_seed + i
        np.random.seed(iter_rand_seed)
        data = sp_distrib.rvs(loc=loc, scale=scale, size=size, random_state=iter_rand_seed)
        if data.min() > min and data.max() < max:
            return data

        # Safety trigger
        i+=1
        # Fall back to safe bounds if min max bounds fails to yield valid values
        if i > max_tries:
            logger.debug("\tCould not find valid values with min max bounds. Fall back to safe bounds")
            logger.debug("\tYou should consider using the `not_bound` option")
            min=0
            max=np.finfo(np.float64).max
        # If too many tries raise an exception
        if i > max_tries*2:
            raise NanocomporeError("Could not find valid data after {} tries".format(i))

def make_mod_dict(intensity_mod, dwell_mod, mod_extend_context):
    """Compute a harmonic series per values depending on the context length"""
    pos_list = list(range(-mod_extend_context, mod_extend_context+1))
    d = OrderedDict()
    d["intensity"] = {i: intensity_mod*(1/(abs(i)+1)) for i in pos_list}
    d["dwell"] = {i: dwell_mod*(1/(abs(i)+1)) for i in pos_list}
    return d

def array_join(sep, array):
    s = ""
    for i in array:
        s+="{}{}".format(i,sep)
    return s[:-len(sep)]

def parse_mod_pos_file(path):
    """ Parses a pos file generated by SimReads()
    and returns a dict of lists where the keys are the ref_ids
    """
    with open(path) as f:
        pos = dict(line.strip().split('\t') for line in f)
    pos.pop('ref_id')
    for k,v in pos.items():
        pos[k]=[int(i) for i in v.split(';')]
    return(pos)
