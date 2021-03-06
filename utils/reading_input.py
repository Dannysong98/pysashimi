#!/usr/bin/env python3
# -*- coding:utf-8 -*-
u"""
Created by ygidtu at 2018.12.19

Inspired by SplicePlot -> mRNAObjects
"""
import os
import re
import traceback

from collections import OrderedDict
from multiprocessing import Pool

import pysam

from tqdm import tqdm

from conf.logger import logger
from src.BamInfo import BamInfo
from src.GenomicLoci import GenomicLoci
from src.ReadDepth import ReadDepth
from src.SpliceRegion import SpliceRegion
from utils.utils import clean_star_filename, is_gtf


def index_gtf(input_gtf, sort_gtf=True, retry=0):
    u"""
    Created by ygidtu

    Extract only exon tags and keep it clean

    :param input_gtf: path to input gtf file
    :param sort_gtf: Boolean value, whether to sort gtf file first
    :param retry: only try to sort gtf once
    :return path to compressed and indexed bgzipped gtf file
    """
    gtf = is_gtf(input_gtf)

    if gtf % 10 != 1:
        raise ValueError("gtf file required, %s seems not a valid gtf file" % input_gtf)

    index = False
    if gtf // 10 > 0:
        output_gtf = input_gtf
    else:
        output_gtf = input_gtf + ".gz"
    if not os.path.exists(output_gtf) or not os.path.exists(output_gtf + ".tbi"):
        index = True

    elif os.path.getctime(output_gtf) < os.path.getctime(output_gtf) or \
            os.path.getctime(output_gtf) < os.path.getctime(output_gtf):
        index = True

    # 2018.12.21 used to handle gtf not sorted error
    if sort_gtf and retry > 1:
        raise OSError("Create index for %s failed, and trying to sort it failed too" % input_gtf)
    elif sort_gtf:
        data = []

        logger.info("Sorting %s" % input_gtf)

        old_input_gtf = input_gtf
        input_gtf = re.sub("\.gtf$", "", input_gtf) + ".sorted.gtf"

        output_gtf = input_gtf + ".gz"

        if os.path.exists(input_gtf) and os.path.exists(output_gtf):
            return output_gtf

        try:
            w = open(input_gtf, "w+")
        except IOError as err:
            w = open("/tmp/sorted.gtf")

        with open(old_input_gtf) as r:
            for line in tqdm(r):
                if line.startswith("#"):
                    w.write(line)
                    continue

                lines = line.split()

                if len(lines) < 1:
                    continue

                data.append(
                    GenomicLoci(
                        chromosome=lines[0],
                        start=lines[3],
                        end=lines[4],
                        strand=lines[6],
                        gtf_line=line
                    )
                )

        for i in sorted(data):
            w.write(i.gtf_line)

        w.close()

    if index:
        logger.info("Create index for %s", input_gtf)
        try:
            pysam.tabix_index(
                input_gtf,
                preset="gff",
                force=True,
                keep_original=True
            )
        except OSError as err:

            if re.search("could not open", str(err)):
                raise err

            logger.error(err)
            logger.error("Guess gtf needs to be sorted")
            return index_gtf(input_gtf=input_gtf, sort_gtf=True, retry=retry + 1)

    return output_gtf


def read_transcripts(gtf_file, region, genome=None, retry=0):
    u"""
    Read transcripts from tabix indexed gtf files

    The original function check if the junctions corresponding to any exists exons, I disable this here

    :param gtf_file: path to bgzip gtf files (with tabix index), only ordered exons in this gtf file
    :param region: splice region
    :param retry: if the gtf chromosome and input chromosome does not match. eg: chr9:1-100:+ <-> 9:1-100:+
    :param genome: path to genome fasta file
    :return: SpliceRegion
    """
    if not os.path.exists(gtf_file):
        raise FileNotFoundError("%s not found" % gtf_file)

    try:
        logger.info("Reading from %s" % gtf_file)

        if genome:
            with pysam.FastaFile(genome) as fa:
                region.sequence = fa.fetch(region.chromosome, region.start - 1, region.end + 1)

        with pysam.Tabixfile(gtf_file, 'r') as gtf_tabix:
            relevant_exons_iterator = gtf_tabix.fetch(
                region.chromosome,
                region.start - 1,
                region.end + 1,
                parser=pysam.asGTF()
            )

            # min_exon_start, max_exon_end, exons_list = float("inf"), float("-inf"),  []
            for line in relevant_exons_iterator:
                try:
                    region.add_gtf(line)
                except IndexError as err:
                    logger.error(err)

    except ValueError as err:
        logger.warn(err)

        # handle the mismatch of chromosomes here
        if retry < 2:
            if not region.chromosome.startswith("chr"):
                logger.info("Guess need 'chr'")
                region.chromosome = "chr" + region.chromosome
            else:
                logger.info("Guess 'chr' is redundant")
                region.chromosome = region.chromosome.replace("chr", "")

            return read_transcripts(gtf_file=gtf_file, region=region, retry=retry + 1)

    return region


def __read_from_bam__(args):
    splice_region, bam, threshold, log, idx = args

    try:
        # print(bam)
        tmp = ReadDepth.determine_depth(
            bam_file_path=bam.path,
            chrm=splice_region.chromosome,
            start_coord=splice_region.start,
            end_coord=splice_region.end,
            threshold=threshold,
            log=log
        )

        tmp.sequence = splice_region.sequence

        if tmp is None:
            return None

        tmp.shrink(
            new_low=splice_region.start,
            new_high=splice_region.end
        )

        return [{bam: tmp}, idx]
    except (OSError, IOError):
        return None


def read_reads_depth_from_bam(bam_list, splice_region, threshold=0, log=None, n_jobs=1):
    u"""
    read reads coverage info from all bams
    :param bam_list: namedtuple (alias, title, path, label)
    :param splice_region: SpliceRegion
    :param threshold: filter low abundance junctions
    :param log
    :param n_jobs
    :return: dict {alias, ReadDepth}
    """
    logger.info("Reading from bam files")
    assert isinstance(splice_region, SpliceRegion), "splice_region should be SplcieRegion, not %s" % type(splice_region)

    res = OrderedDict()

    try:
        # not using multiprocessing when only single process, in case the data size limitation of pickle issue
        if n_jobs == 1:
            for i in [[splice_region, bam, threshold, log, idx] for idx, bam in enumerate(bam_list)]:
                # print(i)
                res.update(__read_from_bam__(i)[0])
        else:
            with Pool(min(n_jobs, len(bam_list))) as p:
                temp = p.map(__read_from_bam__, [[splice_region, bam, threshold, log, idx] for idx, bam in enumerate(bam_list)])

                temp = [x for x in temp if x is not None]
                temp = sorted(temp, key=lambda x: x[1])
                for i in temp:
                    if i is None:
                        continue
                    res.update(i[0])
    except Exception as err:
        logger.error(err)
        traceback.print_exc()
        exit(err)

    if len(res) == 0:
        logger.error("Error reading files, cannot read anything")
        exit(1)

    return res


def read_reads_depth_from_count_table(
        count_table,
        splice_region,
        required,
        colors,
        threshold=0
):
    u"""
    Read junction counts from count_table
    :param count_table: path to count table
    :param splice_region:
    :param required: list of str, which columns are required to draw
    :param threshold: threshold to filter out low abundance junctions
    :param colors: {key: color}
    :return: {label: ReadDepth}
    """

    data = {}
    header = {}
    with open(count_table) as r:
        for line in r:
            lines = line.split()

            if not header:
                for i, j in enumerate(lines):
                    header[i] = clean_star_filename(j)
            else:
                # check file header, to avoide file format error
                if len(header) == len(lines) - 1:
                    logger.info("Change header index due to: Number of headers == number of columns - 1")
                    new_header = {k + 1: v for k, v in header.items()}
                    header = new_header

                for i, j in enumerate(lines):
                    if i == 0:
                        tmp = GenomicLoci.create_loci(lines[0])

                        if not splice_region.is_overlap(tmp):
                            break
                    else:
                        key = header[i]
                        if required:
                            if header[i] in required.keys():
                                key = required[header[i]]
                            else:
                                continue

                        tmp_junctions = data[key] if key in data.keys() else {}

                        if j != "NA" and int(j) >= threshold:
                            tmp_junctions[lines[0]] = int(j)

                        data[key] = tmp_junctions

    res = {}
    for key, value in data.items():

        # customized junctions will introduce string type of key, and list of colors
        # use this try catch to convert key to index to assign colors
        try:
            color = colors[key]
        except TypeError:
            color = colors[len(res) % len(colors)]

        key = BamInfo(
            path=None,
            alias=key,
            label=None,
            title="",
            color=color
        )

        res[key] = ReadDepth.create_depth(value, splice_region)

        res[key].shrink(splice_region.start, splice_region.end)

    return res


if __name__ == '__main__':
    pass
