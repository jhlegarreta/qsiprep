#!/usr/bin/env python
# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
qsiprep base reconstruction workflows
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. autofunction:: init_qsirecon_wf
.. autofunction:: init_single_subject_wf

"""

import os
import os.path as op
from glob import glob
from copy import deepcopy
from nipype import __version__ as nipype_ver
import nipype.pipeline.engine as pe
from nipype.utils.filemanip import split_filename
from nilearn import __version__ as nilearn_ver
from dipy import __version__ as dipy_ver
from pkg_resources import resource_filename as pkgrf
from ...engine import Workflow
from ...utils.sloppy_recon import make_sloppy
from ...__about__ import __version__
from ...interfaces.bids import QsiReconIngress
import logging
import json
from bids.layout import BIDSLayout
from .build_workflow import init_dwi_recon_workflow
from .anatomical import init_recon_anatomical_wf, init_dwi_recon_anatomical_workflow
from ...interfaces.interchange import (anatomical_workflow_outputs, recon_workflow_anatomical_input_fields,
                                       ReconWorkflowInputs,
                                       qsiprep_output_names, recon_workflow_input_fields)

LOGGER = logging.getLogger('nipype.workflow')


def init_qsirecon_wf(subject_list, run_uuid, work_dir, output_dir, recon_input,
                     recon_spec, low_mem, omp_nthreads, sloppy, freesurfer_input,
                     b0_threshold, skip_odf_plots, name="qsirecon_wf"):
    """
    This workflow organizes the execution of qsiprep, with a sub-workflow for
    each subject.

    .. workflow::
        :graph2use: orig
        :simple_form: yes

        from qsiprep.workflows.recon.base import init_qsirecon_wf
        wf = init_qsirecon_wf(subject_list=['test'],
                              run_uuid='X',
                              work_dir='.',
                              recon_input='.',
                              recon_spec='doctest_spec.json',
                              output_dir='.',
                              low_mem=False,
                              freesurfer_input="freesurfer",
                              sloppy=False,
                              omp_nthreads=1,
                              skip_odf_plots=False
                              )


    Parameters

        subject_list : list
            List of subject labels
        run_uuid : str
            Unique identifier for execution instance
        work_dir : str
            Directory in which to store workflow execution state and temporary
            files
        output_dir : str
            Directory in which to save derivatives
        recon_input : str
            Root directory of the output from qsiprep
        recon_spec : str
            Path to a JSON file that specifies how to run reconstruction
        low_mem : bool
            Write uncompressed .nii files in some cases to reduce memory usage
        freesurfer_input : Pathlib.Path
            Path to the directory containing subject freesurfer outputs ($SUBJECTS_DIR)
        sloppy : bool
            If True, replace reconstruction options with fast but bad options.
    """
    qsiprep_wf = Workflow(name=name)
    qsiprep_wf.base_dir = work_dir

    reportlets_dir = os.path.join(work_dir, 'reportlets')
    for subject_id in subject_list:
        single_subject_wf = init_single_subject_wf(
            subject_id=subject_id,
            recon_input=recon_input,
            recon_spec=recon_spec,
            name="single_subject_" + subject_id + "_recon_wf",
            reportlets_dir=reportlets_dir,
            output_dir=output_dir,
            omp_nthreads=omp_nthreads,
            low_mem=low_mem,
            sloppy=sloppy,
            b0_threshold=b0_threshold,
            freesurfer_input=freesurfer_input,
            skip_odf_plots=skip_odf_plots
            )

        single_subject_wf.config['execution']['crashdump_dir'] = (os.path.join(
            output_dir, "qsirecon", "sub-" + subject_id, 'log', run_uuid))
        for node in single_subject_wf._get_all_nodes():
            node.config = deepcopy(single_subject_wf.config)

        qsiprep_wf.add_nodes([single_subject_wf])

    return qsiprep_wf


def init_single_subject_wf(
        subject_id, name, reportlets_dir, output_dir, freesurfer_input, skip_odf_plots,
        low_mem, omp_nthreads, recon_input, recon_spec, sloppy, b0_threshold):
    """
    This workflow organizes the reconstruction pipeline for a single subject.
    Reconstruction is performed using a separate workflow for each dwi series.

    Parameters

        subject_id : str
            List of subject labels
        name : str
            Name of workflow
        low_mem : bool
            Write uncompressed .nii files in some cases to reduce memory usage
        omp_nthreads : int
            Maximum number of threads an individual process may use
        reportlets_dir : str
            Directory in which to save reportlets
        output_dir : str
            Directory in which to save derivatives
        freesurfer_input : Pathlib.Path
            Path to the directory containing subject freesurfer outputs ($SUBJECTS_DIR)
        recon_input : str
            Root directory of the output from qsiprep
        recon_spec : str
            Path to a JSON file that specifies how to run reconstruction
        sloppy : bool
            Use bad parameters for reconstruction to make the workflow faster.
    """
    if name in ('single_subject_wf', 'single_subject_test_recon_wf'):
        # a fake spec
        spec = {"name": "fake",
                "atlases": [],
                "space": "T1w",
                "anatomical": [],
                "nodes": []}
        space = spec['space']
        # for documentation purposes
        dwi_files = ['/made/up/outputs/sub-X_dwi.nii.gz']
        layout = None
    else:
        # If recon_input is specified without qsiprep, check if we can find the subject dir
        subject_dir = 'sub-' + subject_id
        if not op.exists(op.join(recon_input, subject_dir)):
            qp_recon_input = op.join(recon_input, "qsiprep")
            LOGGER.info("%s not in %s, trying recon_input=%s",
                        subject_dir, recon_input, qp_recon_input)
            if not op.exists(op.join(qp_recon_input, subject_dir)):
                raise Exception(
                    "Unable to find subject directory in %s or %s" % (
                        recon_input, qp_recon_input))
            recon_input = qp_recon_input

        spec = _load_recon_spec(recon_spec, sloppy=sloppy)
        space = spec['space']
        layout = BIDSLayout(recon_input, validate=False, absolute_paths=True)
        # Get all the output files that are in this space
        dwi_files = [f.path for f in
                     layout.get(suffix="dwi", subject=subject_id, absolute_paths=True,
                                extension=['nii', 'nii.gz'])
                     if 'space-' + space in f.filename]
        LOGGER.info("found %s in %s", dwi_files, recon_input)

        # Find the corresponding mask files

    workflow = Workflow('sub-{}_{}'.format(subject_id, spec['name']))
    workflow.__desc__ = """
Reconstruction was
performed using *QSIprep* {qsiprep_ver},
which is based on *Nipype* {nipype_ver}
(@nipype1; @nipype2; RRID:SCR_002502).

""".format(
        qsiprep_ver=__version__, nipype_ver=nipype_ver)
    workflow.__postdesc__ = """

Many internal operations of *qsiprep* use
*Nilearn* {nilearn_ver} [@nilearn, RRID:SCR_001362] and
*Dipy* {dipy_ver}[@dipy].
For more details of the pipeline, see [the section corresponding
to workflows in *qsiprep*'s documentation]\
(https://qsiprep.readthedocs.io/en/latest/workflows.html \
"qsiprep's documentation").


### References

    """.format(nilearn_ver=nilearn_ver, dipy_ver=dipy_ver)

    if len(dwi_files) == 0:
        LOGGER.info("No dwi files found for %s", subject_id)
        return workflow

    anat_ingress_wf, available_anatomical_data = init_recon_anatomical_wf(
        subject_id=subject_id,
        recon_input_dir=recon_input,
        extras_to_make=spec.get('anatomical', []),
        freesurfer_dir=freesurfer_input,
        name='anat_ingress_wf')

    # Connect the anatomical-only inputs. NOTE this is not to the inputnode!
    LOGGER.info("Anatomical (T1w) available for recon: %s", available_anatomical_data)

    # Fill-in datasinks and reportlet datasinks for the anatomical workflow
    for _node in anat_ingress_wf.list_node_names():
        node_suffix = _node.split('.')[-1]
        if node_suffix.startswith('ds'):
            base_dir = reportlets_dir if "report" in node_suffix else output_dir
            anat_ingress_wf.get_node(_node).inputs.base_directory = base_dir
            anat_ingress_wf.get_node(_node).inputs.source_file = \
                "anat/sub-{}_desc-preproc_T1w.nii.gz".format(subject_id)

    # Get the anatomical data (masks, atlases, etc)
    atlas_names = spec.get('atlases', [])

    # create a processing pipeline for the dwis in each session
    dwi_recon_wfs = {}
    dwi_individual_anatomical_wfs = {}
    recon_full_inputs = {}
    dwi_ingress_nodes = {}
    for dwi_file in dwi_files:
        wf_name = _get_wf_name(dwi_file)

        # Get the preprocessed DWI and all the related preprocessed images
        dwi_ingress_nodes[dwi_file] = pe.Node(
            QsiReconIngress(dwi_file=dwi_file),
            name=wf_name + "_ingressed_dwi_data")

        # Create scan-specific anatomical data (mask, atlas configs, odf ROIs for reports)
        dwi_individual_anatomical_wfs[dwi_file], dwi_available_anatomical_data = \
            init_dwi_recon_anatomical_workflow(
                atlas_names=atlas_names,
                omp_nthreads=omp_nthreads,
                infant_mode=False,
                prefer_dwi_mask=False,
                sloppy=sloppy,
                b0_threshold=b0_threshold,
                freesurfer_dir=freesurfer_input,
                extras_to_make=spec.get('anatomical', []),
                name=wf_name + "_anat_wf",
                **available_anatomical_data)

        # This node holds all the inputs that will go to the recon workflow.
        # It is the definitive place to check what the input files are
        recon_full_inputs[dwi_file] = pe.Node(ReconWorkflowInputs(), name=wf_name + "_recon_inputs")

        # This is the actual recon workflow for this dwi file
        dwi_recon_wfs[dwi_file] = init_dwi_recon_workflow(
            available_anatomical_data=dwi_available_anatomical_data,
            workflow_spec=spec,
            name=wf_name + "_recon_wf",
            reportlets_dir=reportlets_dir,
            output_dir=output_dir,
            omp_nthreads=omp_nthreads,
            skip_odf_plots=skip_odf_plots)

        # Connect the collected diffusion data (gradients, etc) to the inputnode
        workflow.connect([

            # The dwi data
            (dwi_ingress_nodes[dwi_file], recon_full_inputs[dwi_file], [
                (trait, trait) for trait in qsiprep_output_names]),

            # subject anatomical data to dwi
            (anat_ingress_wf, dwi_individual_anatomical_wfs[dwi_file],
             [("outputnode."+trait, "inputnode."+trait) for trait in anatomical_workflow_outputs]),
            (dwi_ingress_nodes[dwi_file], dwi_individual_anatomical_wfs[dwi_file],
             [(trait, "inputnode." + trait) for trait in qsiprep_output_names]),

            # subject dwi-specific anatomical to recon inputs
            (dwi_individual_anatomical_wfs[dwi_file], recon_full_inputs[dwi_file], [
                ("outputnode." + trait, trait) for trait in recon_workflow_anatomical_input_fields]),

            # recon inputs to recon workflow
            (recon_full_inputs[dwi_file], dwi_recon_wfs[dwi_file],
             [(trait, "inputnode." + trait) for trait in recon_workflow_input_fields])
        ])

    return workflow


def _get_wf_name(dwi_file):
    basedir, fname, ext = split_filename(dwi_file)
    tokens = fname.split("_")
    return "_".join(tokens[:-1]).replace("-", "_")


def _load_recon_spec(spec_name, sloppy=False):
    prepackaged_dir = pkgrf("qsiprep", "data/pipelines")
    prepackaged = [op.split(fname)[1][:-5] for fname in glob(prepackaged_dir+"/*.json")]
    if op.exists(spec_name):
        recon_spec = spec_name
    elif spec_name in prepackaged:
        recon_spec = op.join(prepackaged_dir + "/{}.json".format(spec_name))
    else:
        raise Exception("{} is not a file that exists or in {}".format(spec_name, prepackaged))
    with open(recon_spec, "r") as f:
        try:
            spec = json.load(f)
        except Exception:
            raise Exception("Unable to read JSON spec. Check the syntax.")
    if sloppy:
        LOGGER.warning("Forcing reconstruction to use unrealistic parameters")
        spec = make_sloppy(spec)
    return spec
