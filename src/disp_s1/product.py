"""Module for creating the OPERA output product in NetCDF format."""

from __future__ import annotations

import datetime
import logging
from collections.abc import Iterable, Mapping
from concurrent.futures import ProcessPoolExecutor
from io import StringIO
from multiprocessing import get_context
from pathlib import Path
from typing import Any, Literal, NamedTuple, Optional, Sequence, Union

import h5netcdf
import h5py
import numpy as np
import pyproj
from dolphin import __version__ as dolphin_version
from dolphin import filtering, io
from dolphin._types import Filename
from dolphin.io import round_mantissa
from dolphin.utils import DummyProcessPoolExecutor, format_dates
from dolphin.workflows import DisplacementWorkflow, YamlModel
from numpy.typing import ArrayLike, DTypeLike
from opera_utils import (
    OPERA_DATASET_NAME,
    filter_by_burst_id,
    filter_by_date,
    get_dates,
    get_radar_wavelength,
    get_zero_doppler_time,
    parse_filename,
)

from . import __version__ as disp_s1_version
from ._baselines import _interpolate_data, compute_baselines
from ._common import DATETIME_FORMAT
from ._reference import ReferencePoint
from .browse_image import make_browse_image_from_arr
from .pge_runconfig import AlgorithmParameters, RunConfig
from .product_info import DISPLACEMENT_PRODUCTS, ProductInfo
from .solid_earth_tides import calculate_solid_earth_tides_correction

logger = logging.getLogger(__name__)

CORRECTIONS_GROUP_NAME = "corrections"
IDENTIFICATION_GROUP_NAME = "identification"
METADATA_GROUP_NAME = "metadata"
GLOBAL_ATTRS = {
    "Conventions": "CF-1.8",
    "contact": "opera-sds-ops@jpl.nasa.gov",
    "institution": "NASA JPL",
    "mission_name": "OPERA",
    "reference_document": "JPL D–108765",
    "title": "OPERA_L3_DISP-S1 Product",
}

# Use the "paging file space strategy"
# https://docs.h5py.org/en/stable/high/file.html#h5py.File
# Page size should be larger than the largest chunk in the file
FILE_OPTS = {"fs_strategy": "page", "fs_page_size": 2**22}
CHUNK_SHAPE = (128, 128)

# Convert chunks to a tuple or h5py errors
HDF5_OPTS = io.DEFAULT_HDF5_OPTIONS.copy()
HDF5_OPTS["chunks"] = tuple(CHUNK_SHAPE)  # type: ignore
# The GRID_MAPPING_DSET variable is used to store the name of the dataset containing
# the grid mapping information, which includes the coordinate reference system (CRS)
# and the GeoTransform. This is in accordance with the CF 1.8 conventions for adding
# geospatial metadata to NetCDF files.
# http://cfconventions.org/cf-conventions/cf-conventions.html#grid-mappings-and-projections
# Note that the name "spatial_ref" used here is arbitrary, but it follows the default
# used by other libraries, such as rioxarray:
# https://github.com/corteva/rioxarray/blob/5783693895b4b055909c5758a72a5d40a365ef11/rioxarray/rioxarray.py#L34 # noqa
GRID_MAPPING_DSET = "spatial_ref"

COMPRESSED_SLC_TEMPLATE = "compressed_{burst_id}_{date_str}.h5"


def create_output_product(
    output_name: Filename,
    unw_filename: Filename,
    conncomp_filename: Filename,
    temp_coh_filename: Filename,
    ifg_corr_filename: Filename,
    ps_mask_filename: Filename,
    shp_count_filename: Filename,
    similarity_filename: Filename,
    water_mask_filename: Filename | None,
    pge_runconfig: RunConfig,
    dolphin_config: DisplacementWorkflow,
    reference_cslc_files: Sequence[Filename],
    secondary_cslc_files: Sequence[Filename],
    los_east_file: Filename | None = None,
    los_north_file: Filename | None = None,
    near_far_incidence_angles: tuple[float, float] = (30.0, 45.0),
    reference_point: ReferencePoint | None = None,
    corrections: Optional[dict[str, ArrayLike]] = None,
):
    """Create the OPERA output product in NetCDF format.

    Parameters
    ----------
    output_name : Filename, optional
        The path to the output NetCDF file.
    unw_filename : Filename
        The path to the input unwrapped phase image.
    conncomp_filename : Filename
        The path to the input connected components image.
    temp_coh_filename : Filename
        The path to the input temporal coherence image.
    ifg_corr_filename : Filename
        The path to the input interferometric correlation image.
    ps_mask_filename : Filename
        The path to the input persistent scatterer mask image.
    shp_count_filename : Filename
        The path to statistically homogeneous pixels (SHP) counts.
    similarity_filename : Filename
        The path to the cosine similarity image.
    water_mask_filename : Filename, optional
        Path to the binary water mask to use in creating a recommended mask.
    pge_runconfig : Optional[RunConfig], optional
        The PGE run configuration, by default None
        Used to add extra metadata to the output file.
    dolphin_config : dolphin.workflows.DisplacementWorkflow
        Configuration object run by `dolphin`.
    reference_cslc_files : Sequence[Filename]
        Input CSLC products corresponding to the reference date.
        Used for metadata generation.
    secondary_cslc_files : Sequence[Filename]
        Input CSLC products corresponding to the secondary date.
        Used for metadata generation.
    los_east_file : Path, optional
        Path to the east component of line of sight unit vector
    los_north_file : Path, optional
        Path to the north component of line of sight unit vector
    near_far_incidence_angles : tuple[float, float]
        Tuple of near range incidence angle, far range incidence angle.
        If not specified, uses approximate Sentinel-1 values of (30.0, 45.0)
    reference_point : ReferencePoint, optional
        Named tuple with (row, col, lat, lon) of selected reference pixel.
        If None, will record empty in the dataset's attributes
    corrections : dict[str, ArrayLike], optional
        A dictionary of corrections to write to the output file, by default None

    """
    if corrections is None:
        corrections = {}
    algorithm_parameters = AlgorithmParameters.from_yaml(
        pge_runconfig.dynamic_ancillary_file_group.algorithm_parameters_file
    )

    crs = io.get_raster_crs(unw_filename)
    gt = io.get_raster_gt(unw_filename)
    cols, rows = io.get_raster_xysize(unw_filename)
    shape = (rows, cols)

    if len(reference_cslc_files) == 0:
        raise ValueError("Missing input reference cslc files")
    if len(secondary_cslc_files) == 0:
        raise ValueError("Missing input secondary cslc files")

    def _get_start_end_cslcs(files):
        if len(files) == 1:
            start = end = files[0]
        else:
            # Sorting by name means the earlier Burst IDs come first.
            # Since the Burst Ids are numbered in increasing order of acquisition time,
            # This is also valid to get the start/end bursts within the frame.
            start, *_, end = sorted(files, key=lambda f: Path(f).name)
        logger.debug(f"Start, end files: {start}, {end}")
        return start, end

    reference_start_file, reference_end_file = _get_start_end_cslcs(
        reference_cslc_files
    )
    reference_start_time = get_zero_doppler_time(reference_start_file, type_="start")
    reference_end_time = get_zero_doppler_time(reference_end_file, type_="end")

    secondary_start, secondary_end = _get_start_end_cslcs(secondary_cslc_files)
    secondary_start_time = get_zero_doppler_time(secondary_start, type_="start")
    secondary_end_time = get_zero_doppler_time(secondary_end, type_="end")

    radar_wavelength = get_radar_wavelength(reference_cslc_files[0])
    phase2disp = -1 * float(radar_wavelength) / (4.0 * np.pi)

    y, x = _create_yx_arrays(gt=gt, shape=shape)
    # TODO: do we need all corrections/smaller grids to be same subsample factor?
    subsample = 50
    y, x = y[::subsample], x[::subsample]
    try:
        logger.info("Calculating perpendicular baselines subsampled by %s", subsample)
        baseline_arr = compute_baselines(
            reference_start_file,
            secondary_start,
            x=x,
            y=y,
            epsg=crs.to_epsg(),
            height=0,
        )
    except Exception:
        logger.error(
            f"Failed to compute baselines for {reference_start_file},"
            f" {secondary_start}",
            exc_info=True,
        )
        baseline_arr = np.zeros((100, 100))
    corrections["baseline"] = _interpolate_data(baseline_arr, shape=shape)

    logger.info("Extracting data footprint")
    try:
        footprint_wkt = extract_footprint(raster_path=unw_filename)
    except Exception:
        logger.error("Failed to extract raster footprint", exc_info=True)
        footprint_wkt = ""
    # Get bounds for "Bounding box corners"
    bounds = io.get_raster_bounds(unw_filename)

    # Load and process unwrapped phase data, needs more custom masking
    unw_arr_ma = io.load_gdal(unw_filename, masked=True)
    unw_arr = np.ma.filled(unw_arr_ma, 0)
    mask = unw_arr == 0

    input_units = io.get_raster_units(unw_filename)
    if not input_units or input_units not in ("meters", "radians"):
        logger.warning(f"Unknown units for {unw_filename}: assuming radians")
        disp_arr = unw_arr * phase2disp
    elif input_units == "radians":
        disp_arr = unw_arr * phase2disp
    else:
        disp_arr = unw_arr

    _, x_res, _, _, _, y_res = gt
    # Average for the pixel spacing for filtering
    pixel_spacing = (abs(x_res) + abs(y_res)) / 2
    wavelength_cutoff = algorithm_parameters.spatial_wavelength_cutoff
    logger.info(
        "Creating short wavelength displacement product with %s meter cutoff",
        wavelength_cutoff,
    )

    # Create the commended mask:
    temporal_coherence = io.load_gdal(temp_coh_filename, masked=True)
    # Get summary statistics on the layers for CMR filtering/searching purposes
    average_temporal_coherence = temporal_coherence.mean()

    if water_mask_filename:
        water_mask_data = io.load_gdal(water_mask_filename, masked=True).filled(0)
        is_water = water_mask_data == 0
    else:
        # Not provided: Don't indicate anything is water in this mask.
        is_water = np.zeros(temporal_coherence.shape, dtype=bool)

    conncomps = io.load_gdal(conncomp_filename, masked=True).filled(0)
    similarity = io.load_gdal(similarity_filename, masked=True).filled(0)

    # Mark pixels that are bad
    is_zero_conncomp = conncomps == 0
    bad_temporal_coherence = temporal_coherence < 0.6
    bad_similarity = similarity < 0.5
    is_low_quality = bad_temporal_coherence & bad_similarity

    # If a pixel has any of the reasons to be bad, recommend masking
    bad_pixel_mask = is_water | is_zero_conncomp | is_low_quality
    # Note: An alternate way to view this:
    # good_conncomp & is_no_water & (good_temporal_coherence | good_similarity)
    recommended_mask = np.logical_not(bad_pixel_mask)
    del temporal_coherence, conncomps, similarity

    filtered_disp_arr = filtering.filter_long_wavelength(
        unwrapped_phase=disp_arr,
        bad_pixel_mask=bad_pixel_mask,
        wavelength_cutoff=wavelength_cutoff,
        pixel_spacing=pixel_spacing,
    ).astype(np.float32)
    DISPLACEMENT_PRODUCTS.short_wavelength_displacement.attrs |= {
        "wavelength_cutoff": str(wavelength_cutoff)
    }

    disp_arr[mask] = np.nan
    # Be more aggressive with the short wavelength displacement mask:
    filtered_disp_arr[bad_pixel_mask] = np.nan

    product_infos: list[ProductInfo] = list(DISPLACEMENT_PRODUCTS)

    with h5netcdf.File(output_name, "w", **FILE_OPTS) as f:
        f.attrs.update(GLOBAL_ATTRS)
        _create_grid_mapping(group=f, crs=crs, gt=gt)

        _create_yx_dsets(group=f, gt=gt, shape=shape, include_time=True)
        _create_time_dset(
            group=f,
            time=secondary_start_time,
            long_name="Time corresponding to beginning of secondary acquisition",
            variable_name="time",
        )
        _create_time_dset(
            group=f,
            time=reference_start_time,
            long_name="Time corresponding to beginning of reference acquisition",
            variable_name="reference_time",
        )
        for info, data in zip(product_infos[:2], [disp_arr, filtered_disp_arr]):
            round_mantissa(data, keep_bits=info.keep_bits)
            _create_geo_dataset(
                group=f,
                name=info.name,
                data=data,
                long_name=info.long_name,
                description=info.description,
                fillvalue=info.fillvalue,
                attrs=info.attrs,
            )

        make_browse_image_from_arr(
            output_filename=Path(output_name).with_suffix(
                f".{product_infos[1].name}.png"
            ),
            arr=filtered_disp_arr,
            mask=recommended_mask,
            vmin=algorithm_parameters.browse_image_vmin_vmax[0],
            vmax=algorithm_parameters.browse_image_vmin_vmax[1],
        )
        del disp_arr
        del filtered_disp_arr

        # Add the recommended mask, which is already loaded
        info = product_infos[2]
        _create_geo_dataset(
            group=f,
            name=info.name,
            data=recommended_mask.astype("uint8"),
            description=info.description,
            long_name=info.long_name,
            fillvalue=info.fillvalue,
            attrs=info.attrs,
        )

        # For the others, load and save each individually
        data_files = [
            conncomp_filename,
            temp_coh_filename,
            ifg_corr_filename,
            ps_mask_filename,
            shp_count_filename,
            water_mask_filename,
            similarity_filename,
        ]

        for info, filename in zip(product_infos[3:], data_files, strict=True):
            if filename is not None and Path(filename).exists():
                data = io.load_gdal(filename).astype(info.dtype)
            else:
                data = np.full(shape=shape, fill_value=info.fillvalue, dtype=info.dtype)

            if info.keep_bits is not None:
                round_mantissa(data, keep_bits=info.keep_bits)

            _create_geo_dataset(
                group=f,
                name=info.name,
                data=data,
                description=info.description,
                long_name=info.long_name,
                fillvalue=info.fillvalue,
                attrs=info.attrs,
            )

    if los_east_file is not None and los_north_file is not None:
        logger.info("Calculating solid earth tide")
        ref_tuple = (
            (reference_point.row, reference_point.col) if reference_point else None
        )
        orbit_direction = _get_orbit_direction(reference_cslc_files[0])
        solid_earth_los = calculate_solid_earth_tides_correction(
            like_filename=unw_filename,
            reference_start_time=reference_start_time,
            reference_stop_time=reference_end_time,
            secondary_start_time=secondary_start_time,
            secondary_stop_time=secondary_end_time,
            los_east_file=los_east_file,
            los_north_file=los_north_file,
            orbit_direction=orbit_direction,
            reference_point=ref_tuple,
        )
        corrections["solid_earth"] = solid_earth_los

    _create_corrections_group(
        output_name=output_name,
        corrections=corrections,
        shape=shape,
        gt=gt,
        crs=crs,
        secondary_start_time=secondary_start_time,
        reference_point=reference_point,
    )

    orbit_type = _get_orbit_type(reference_cslc_files[0])
    _create_identification_group(
        output_name=output_name,
        pge_runconfig=pge_runconfig,
        radar_wavelength=radar_wavelength,
        orbit_type=orbit_type,
        reference_start_time=reference_start_time,
        reference_end_time=reference_end_time,
        secondary_start_time=secondary_start_time,
        secondary_end_time=secondary_end_time,
        footprint_wkt=footprint_wkt,
        product_bounds=tuple(bounds),
        average_temporal_coherence=average_temporal_coherence,
        near_far_incidence_angles=near_far_incidence_angles,
    )

    _create_metadata_group(
        output_name=output_name,
        pge_runconfig=pge_runconfig,
        dolphin_config=dolphin_config,
    )
    copy_cslc_metadata_to_displacement(
        reference_cslc_file=reference_start_file,
        secondary_cslc_file=secondary_start,
        output_disp_file=output_name,
    )


def _create_corrections_group(
    output_name: Filename,
    corrections: dict[str, ArrayLike],
    shape: tuple[int, int],
    gt: list[float],
    crs: pyproj.CRS,
    secondary_start_time: datetime.datetime,
    reference_point: ReferencePoint | None,
) -> None:
    keep_bits = 10
    logger.debug("Rounding mantissa in corrections to %s bits", keep_bits)
    for data in corrections.values():
        # Use same amount of truncation for all correction layers
        if np.issubdtype(data.dtype, np.floating):
            round_mantissa(data, keep_bits=keep_bits)
    logger.info("Creating corrections group in %s", output_name)
    with h5netcdf.File(output_name, "a") as f:
        # Create the group holding phase corrections used on the unwrapped phase
        corrections_group = f.create_group(CORRECTIONS_GROUP_NAME)
        corrections_group.attrs["description"] = (
            "Phase corrections applied to the unwrapped_phase"
        )
        empty_arr = np.zeros(shape, dtype="float32")

        # TODO: Are we going to downsample these for space?
        # if so, they need they're own X/Y variables and GeoTransform
        _create_grid_mapping(group=corrections_group, crs=crs, gt=gt)
        _create_yx_dsets(group=corrections_group, gt=gt, shape=shape, include_time=True)
        _create_time_dset(
            group=corrections_group,
            time=secondary_start_time,
            long_name="Time corresponding to beginning of secondary image",
        )
        ionosphere = corrections.get("ionosphere", empty_arr)
        _create_geo_dataset(
            group=corrections_group,
            name="ionospheric_delay",
            long_name="Ionospheric Delay",
            data=ionosphere,
            description="Ionospheric phase delay used to correct the unwrapped phase",
            fillvalue=np.nan,
            attrs={"units": "meters"},
        )
        solid_earth = corrections.get("solid_earth", empty_arr)
        _create_geo_dataset(
            group=corrections_group,
            name="solid_earth_tide",
            long_name="Solid Earth Tide",
            data=solid_earth,
            description="Solid Earth tide used to correct the unwrapped phase",
            fillvalue=np.nan,
            attrs={"units": "meters"},
        )
        baseline = corrections.get("baseline", empty_arr)
        _create_geo_dataset(
            group=corrections_group,
            name="perpendicular_baseline",
            long_name="Perpendicular Baseline",
            data=baseline,
            description=(
                "Perpendicular baseline between reference and secondary acquisitions"
            ),
            fillvalue=np.nan,
            attrs={"units": "meters"},
        )
        # Make a scalar dataset for the reference point
        if reference_point is not None:
            row, col, lat, lon = reference_point
            ref_attrs = {
                "rows": [row],
                "cols": [col],
                "latitudes": [lat],
                "longitudes": [lon],
                "units": "unitless",
            }
        else:
            ref_attrs = {
                "rows": [],
                "cols": [],
                "latitudes": [],
                "longitudes": [],
                "units": "unitless",
            }
        _create_dataset(
            group=corrections_group,
            name="reference_point",
            dimensions=(),
            data=0,
            fillvalue=0,
            description=(
                "Dummy dataset containing attributes with the locations where the"
                " reference phase was taken."
            ),
            dtype=int,
            # Note: the dataset contains attributes with lists, since the reference
            # could have come from multiple points (e.g. boxcar average of an area).
            attrs=ref_attrs,
        )


def _create_identification_group(
    output_name: Filename,
    pge_runconfig: RunConfig,
    radar_wavelength: float,
    orbit_type: str,
    reference_start_time: datetime.datetime,
    reference_end_time: datetime.datetime,
    secondary_start_time: datetime.datetime,
    secondary_end_time: datetime.datetime,
    footprint_wkt: str,
    product_bounds: tuple[float, float, float, float],
    average_temporal_coherence: float,
    near_far_incidence_angles: tuple[float, float] = (30.0, 45.0),
) -> None:
    """Create the identification group in the output file."""
    with h5netcdf.File(output_name, "a") as f:
        identification_group = f.create_group(IDENTIFICATION_GROUP_NAME)
        _create_dataset(
            group=identification_group,
            name="processing_facility",
            dimensions=(),
            data="NASA Jet Propulsion Laboratory on AWS",
            fillvalue=None,
            description="Product processing facility",
        )
        _create_dataset(
            group=identification_group,
            name="frame_id",
            dimensions=(),
            data=pge_runconfig.input_file_group.frame_id,
            fillvalue=None,
            description="ID number of the processed frame",
        )
        _create_dataset(
            group=identification_group,
            name="product_version",
            dimensions=(),
            data=pge_runconfig.product_path_group.product_version,
            fillvalue=None,
            description="Version of the product",
        )
        _create_dataset(
            group=identification_group,
            name="static_layers_data_access",
            dimensions=(),
            data=pge_runconfig.product_path_group.static_layers_data_access,
            fillvalue=None,
            description=(
                "Location of the static layers product associated with this product"
                " (URL or DOI)"
            ),
        )
        _create_dataset(
            group=identification_group,
            name="radar_band",
            dimensions=(),
            data="C",
            fillvalue=None,
            description="Acquired radar frequency band",
        )

        _create_dataset(
            group=identification_group,
            name="reference_zero_doppler_start_time",
            dimensions=(),
            data=reference_start_time.strftime(DATETIME_FORMAT),
            fillvalue=None,
            description=(
                "Zero doppler start time of the first burst contained in the frame for"
                " the reference acquisition."
            ),
        )
        _create_dataset(
            group=identification_group,
            name="reference_zero_doppler_end_time",
            dimensions=(),
            data=reference_end_time.strftime(DATETIME_FORMAT),
            fillvalue=None,
            description=(
                "Zero doppler start time of the last burst contained in the frame for"
                " the reference acquisition."
            ),
        )
        _create_dataset(
            group=identification_group,
            name="secondary_zero_doppler_start_time",
            dimensions=(),
            data=secondary_start_time.strftime(DATETIME_FORMAT),
            fillvalue=None,
            description=(
                "Zero doppler start time of the first burst contained in the frame for"
                " the secondary acquisition."
            ),
        )
        _create_dataset(
            group=identification_group,
            name="secondary_zero_doppler_end_time",
            dimensions=(),
            data=secondary_end_time.strftime(DATETIME_FORMAT),
            fillvalue=None,
            description=(
                "Zero doppler start time of the last burst contained in the frame for"
                " the secondary acquisition."
            ),
        )

        _create_dataset(
            group=identification_group,
            name="bounding_polygon",
            dimensions=(),
            data=footprint_wkt,
            fillvalue=None,
            description="WKT representation of bounding polygon of the image",
            attrs={"units": "degrees"},
        )

        _create_dataset(
            group=identification_group,
            name="radar_wavelength",
            dimensions=(),
            data=radar_wavelength,
            fillvalue=None,
            description="Wavelength of the transmitted signal",
            attrs={"units": "meters"},
        )

        _create_dataset(
            group=identification_group,
            name="reference_datetime",
            dimensions=(),
            data=reference_start_time.strftime(DATETIME_FORMAT),
            fillvalue=None,
            description=(
                "UTC datetime of the acquisition sensing start of the reference epoch"
                " to which the unwrapped phase is referenced."
            ),
        )
        _create_dataset(
            group=identification_group,
            name="secondary_datetime",
            dimensions=(),
            data=secondary_start_time.strftime(DATETIME_FORMAT),
            fillvalue=None,
            description=(
                "UTC datetime of the acquisition sensing start of current acquisition"
                " used to create the unwrapped phase."
            ),
        )
        _create_dataset(
            group=identification_group,
            name="average_temporal_coherence",
            dimensions=(),
            data=average_temporal_coherence,
            fillvalue=None,
            description="Mean value of valid pixels within temporal_coherence layer.",
            attrs={"units": "unitless"},
        )
        # CEOS: Section 1.3
        _create_dataset(
            group=identification_group,
            name="ceos_analysis_ready_data_product_type",
            dimensions=(),
            data="InSAR",
            fillvalue=None,
            description="CEOS Analysis Ready Data (CARD) product type name",
            attrs={"units": "unitless"},
        )
        # CEOS: Section 1.4
        _create_dataset(
            group=identification_group,
            name="ceos_analysis_ready_data_document_identifier",
            dimensions=(),
            data="https://github.com/ceos-org/",
            fillvalue=None,
            description="CEOS Analysis Ready Data (CARD) document identifier",
            attrs={"units": "unitless"},
        )
        input_dts = sorted(
            [get_dates(f)[0] for f in pge_runconfig.input_file_group.cslc_file_list]
        )
        processing_dts = sorted(
            get_dates(f)[1]
            for f in pge_runconfig.input_file_group.cslc_file_list
            if "compressed" not in str(f).lower()
        )
        parsed_files = [
            parse_filename(f)
            for f in pge_runconfig.input_file_group.cslc_file_list
            if "compressed" not in str(f).lower()
        ]
        input_sensors = {p.get("sensor") for p in parsed_files if p.get("sensor")}

        # CEOS: Section 1.5
        _create_dataset(
            group=identification_group,
            name="source_data_satellite_names",
            dimensions=(),
            data=",".join(input_sensors),
            fillvalue=None,
            description="Names of satellites included in input granules",
            attrs={"units": "unitless"},
        )
        starting_date_str = input_dts[0].isoformat()
        _create_dataset(
            group=identification_group,
            name="source_data_earliest_acquisition",
            dimensions=(),
            data=starting_date_str,
            fillvalue=None,
            description="Datetime of earliest input granule used during processing",
            attrs={"units": "unitless"},
        )
        last_date_str = input_dts[-1].isoformat()
        _create_dataset(
            group=identification_group,
            name="source_data_latest_acquisition",
            dimensions=(),
            data=last_date_str,
            fillvalue=None,
            description="Datetime of latest input granule used during processing",
            attrs={"units": "unitless"},
        )
        early_processing_date_str = processing_dts[0].isoformat()
        _create_dataset(
            group=identification_group,
            name="source_data_earliest_processing_datetime",
            dimensions=(),
            data=early_processing_date_str,
            fillvalue=None,
            description="Earliest processing datetime of input granules",
            attrs={"units": "unitless"},
        )
        last_processing_date_str = processing_dts[-1].isoformat()
        _create_dataset(
            group=identification_group,
            name="source_data_latest_processing_datetime",
            dimensions=(),
            data=last_processing_date_str,
            fillvalue=None,
            description="Latest processing datetime of input granules",
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=identification_group,
            name="ceos_number_of_input_granules",
            dimensions=(),
            data=len(pge_runconfig.input_file_group.cslc_file_list),
            fillvalue=None,
            description="Number of input data granule used during processing.",
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=identification_group,
            name="source_data_orbit_type",
            dimensions=(),
            data=orbit_type,
            fillvalue=None,
            description=(
                "Type of orbit (precise, restituted) used during input data processing"
            ),
            attrs={"units": "unitless"},
        )

        # CEOS: Section 1.6.4 source acquisition parameters
        _create_dataset(
            group=identification_group,
            name="acquisition_mode",
            dimensions=(),
            data="IW",
            fillvalue=None,
            description="Radar acquisition mode for input products",
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=identification_group,
            name="radar_center_frequency",
            dimensions=(),
            data=5405000454.33435,
            fillvalue=None,
            description="Radar center frequency of input products",
            attrs={"units": "Hertz"},
        )
        _create_dataset(
            group=identification_group,
            name="source_data_polarization",
            dimensions=(),
            data="VV",
            fillvalue=None,
            description="Radar polarization of input products",
            attrs={"units": "unitless"},
        )
        # CEOS: Section 1.6.7 source data attributes
        _create_dataset(
            group=identification_group,
            name="source_data_original_institution",
            dimensions=(),
            data="European Space Agency",
            fillvalue=None,
            description="Original processing institution of Sentinel-1 SLC data",
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=identification_group,
            name="source_data_access",
            dimensions=(),
            data="https://search.asf.alaska.edu/#/?dataset=OPERA-S1&productTypes=CSLC",
            fillvalue=None,
            description=(
                "The metadata identifies the location from where the source data can be"
                " retrieved, expressed as a URL or DOI."
            ),
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=identification_group,
            name="source_data_file_list",
            dimensions=(),
            data=",".join(
                p.stem for p in pge_runconfig.input_file_group.cslc_file_list
            ),
            fillvalue=None,
            description=(
                "List of input coregistered SLC granules used to create displacement"
                " frame"
            ),
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=identification_group,
            name="source_data_range_resolutions",
            dimensions=(),
            data="[2.7, 3.1, 3.5]",
            fillvalue=None,
            description=(
                "List of [IW1, IW2, IW3] range resolutions from source L1 Sentinel-1"
                " SLCs"
            ),
            attrs={"units": "meters"},
        )
        _create_dataset(
            group=identification_group,
            name="source_data_azimuth_resolutions",
            dimensions=(),
            data="[22.5, 22.7, 22.6]",
            fillvalue=None,
            description=(
                "List of [IW1, IW2, IW3] azimuth resolutions from L1 Sentinel-1 SLCs"
            ),
            attrs={"units": "meters"},
        )
        _create_dataset(
            group=identification_group,
            name="source_data_x_spacing",
            dimensions=(),
            data=5,
            fillvalue=None,
            description="Pixel spacing of source geocoded SLC data in the x-direction.",
            attrs={"units": "meters"},
        )
        _create_dataset(
            group=identification_group,
            name="source_data_y_spacing",
            dimensions=(),
            data=10,
            fillvalue=None,
            description="Pixel spacing of source geocoded SLC data in the y-direction.",
            attrs={"units": "meters"},
        )
        # Source for the max. NESZ:
        # (https://sentinels.copernicus.eu/web/sentinel/user-guides/
        # 1.6.9
        _create_dataset(
            group=identification_group,
            name="source_data_max_noise_equivalent_sigma_zero",
            dimensions=(),
            data=-22.0,
            fillvalue=None,
            description="Maximum Noise equivalent sigma0 in dB",
            attrs={"units": "dB"},
        )
        _create_dataset(
            group=identification_group,
            name="source_data_dem_name",
            dimensions=(),
            data="Copernicus GLO-30",
            fillvalue=None,
            description=(
                "Name of Digital Elevation Model used during input data processing."
            ),
            attrs={"units": "dB"},
        )
        _create_dataset(
            group=identification_group,
            name="near_range_incidence_angle",
            dimensions=(),
            data=near_far_incidence_angles[0],
            fillvalue=None,
            description="Incidence angle at the near range of the displacement frame",
            attrs={"units": "degrees"},
        )
        _create_dataset(
            group=identification_group,
            name="far_range_incidence_angle",
            dimensions=(),
            data=near_far_incidence_angles[1],
            fillvalue=None,
            description="Incidence angle at the far range of the displacement frame",
            attrs={"units": "degrees"},
        )
        # CEOS: 1.7.3
        _create_dataset(
            group=identification_group,
            name="product_sample_spacing",
            dimensions=(),
            data=30,
            fillvalue=None,
            description=(
                "Spacing between adjacent X/Y samples of displacement product in UTM"
                " coordinates"
            ),
            attrs={"units": "meters"},
        )
        # CEOS: 1.7.7
        _create_dataset(
            group=identification_group,
            name="product_bounding_box",
            dimensions=(),
            data=",".join(map(str, product_bounds)),
            fillvalue=None,
            description=(
                "Opposite corners of the product file in the UTM coordinates as (west,"
                " south, east, north)"
            ),
            attrs={"units": "meters"},
        )
        _create_dataset(
            group=identification_group,
            name="product_data_access",
            dimensions=(),
            data=(
                "https://search.asf.alaska.edu/#/?dataset=OPERA-S1&productTypes=DISP-S1"
            ),
            fillvalue=None,
            description=(
                "The metadata identifies the location from where the source data can be"
                " retrieved, expressed as a URL or DOI."
            ),
            attrs={"units": "unitless"},
        )


def _create_metadata_group(
    output_name: Filename,
    pge_runconfig: RunConfig,
    dolphin_config: DisplacementWorkflow,
) -> None:
    """Create the metadata group in the output file."""
    with h5netcdf.File(output_name, "a") as f:
        metadata_group = f.create_group(METADATA_GROUP_NAME)
        _create_dataset(
            group=metadata_group,
            name="disp_s1_software_version",
            dimensions=(),
            data=disp_s1_version,
            fillvalue=None,
            description="Version of the disp-s1 software used to generate the product.",
        )
        _create_dataset(
            group=metadata_group,
            name="dolphin_software_version",
            dimensions=(),
            data=dolphin_version,
            fillvalue=None,
            description="Version of the dolphin software used to generate the product.",
        )

        def _to_string(model: YamlModel):
            ss = StringIO()
            model.to_yaml(ss)
            return "".join(c for c in ss.getvalue() if ord(c) < 128)

        _create_dataset(
            group=metadata_group,
            name="pge_runconfig",
            dimensions=(),
            data=_to_string(pge_runconfig),
            fillvalue=None,
            description=(
                "The full PGE runconfig YAML file used to generate the product."
            ),
        )
        algo_param_path = (
            pge_runconfig.dynamic_ancillary_file_group.algorithm_parameters_file
        )
        param_str = "".join(c for c in algo_param_path.read_text() if ord(c) < 128)
        _create_dataset(
            group=metadata_group,
            name="algorithm_parameters_yaml",
            dimensions=(),
            data=param_str,
            fillvalue=None,
            description=(
                "The full PGE runconfig YAML file used to generate the product."
            ),
        )
        _create_dataset(
            group=metadata_group,
            name="dolphin_workflow_config",
            dimensions=(),
            data=_to_string(dolphin_config),
            fillvalue=None,
            description=(
                "The configuration parameters used by `dolphin` during the processing."
            ),
        )
        # CEOS 1.7.10
        _create_dataset(
            group=metadata_group,
            name="product_pixel_coordinate_convention",
            dimensions=(),
            data="center",
            fillvalue=None,
            description="x/y coordinate convention referring to pixel center or corner",
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=metadata_group,
            name="product_persistent_scatterer_selection_criteria",
            dimensions=(),
            data="Amplitude Dispersion",
            fillvalue=None,
            description="Name of persistent scatterer selection criteria",
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=metadata_group,
            name="product_persistent_scatterer_selection_criteria_doi",
            dimensions=(),
            data="https://doi.org/10.1109/36.898661",
            fillvalue=None,
            description=(
                "DOI of reference describing persistent scatterer selection criteria"
            ),
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=metadata_group,
            name="phase_unwrapping_method",
            dimensions=(),
            data=str(dolphin_config.unwrap_options.unwrap_method),
            fillvalue=None,
            description="Name of phase unwrapping method",
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=metadata_group,
            name="atmospheric_phase_correction",
            dimensions=(),
            data="None",
            fillvalue=None,
            description="Method used to correct for atmosphere phase noise.",
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=metadata_group,
            name="ionospheric_phase_correction",
            dimensions=(),
            data="None",
            fillvalue=None,
            description="Method used to correct for ionospheric phase noise.",
            attrs={"units": "unitless"},
        )
        _create_dataset(
            group=metadata_group,
            name="ceos_noise_removal",
            dimensions=(),
            data="No",
            fillvalue=None,
            description=(
                "Flag if noise removal* has been applied (Y/N). Metadata should include"
                " the noise removal algorithm and reference to the algorithm as URL or"
                " DOI."
            ),
            attrs={"units": "unitless"},
        )


def _get_orbit_direction(cslc_filename: Filename) -> Literal["ascending", "descending"]:
    with h5py.File(cslc_filename) as hf:
        out = hf["/identification/orbit_pass_direction"][()]
        if isinstance(out, bytes):
            out = out.decode("utf-8")
    return out


def _get_orbit_type(
    cslc_filename: Filename,
) -> Literal["precise orbit file", "restituted orbit file"]:
    with h5py.File(cslc_filename) as hf:
        out = hf["/quality_assurance/orbit_information/orbit_type"][()]
        if isinstance(out, bytes):
            out = out.decode("utf-8")
    return out


def _create_dataset(
    *,
    group: h5netcdf.Group,
    name: str,
    dimensions: Optional[Sequence[str]],
    data: Union[np.ndarray, str],
    description: str,
    fillvalue: Optional[float],
    long_name: str | None = None,
    attrs: Optional[dict[str, Any]] = None,
    dtype: Optional[DTypeLike] = None,
) -> h5netcdf.Variable:
    if attrs is None:
        attrs = {}
    attrs.update(description=description)
    if long_name:
        attrs["long_name"] = long_name

    options = HDF5_OPTS
    if isinstance(data, str):
        options = {}
        # This is a string, so we need to convert it to bytes or it will fail
        data = np.bytes_(data)
    elif np.array(data).size <= 1:
        # Scalars don't need chunks/compression
        options = {}
    dset = group.create_variable(
        name,
        dimensions=dimensions,
        data=data,
        dtype=dtype,
        fillvalue=fillvalue,
        **options,
    )
    dset.attrs.update(attrs)
    return dset


def _create_geo_dataset(
    *,
    group: h5netcdf.Group,
    name: str,
    data: np.ndarray,
    long_name: str,
    description: str,
    fillvalue: float,
    attrs: Optional[dict[str, Any]],
    include_time: bool = False,
    x_name: str = "x",
    y_name: str = "y",
    grid_mapping_dset_name=GRID_MAPPING_DSET,
) -> h5netcdf.Variable:
    if include_time:
        dimensions = ["time", y_name, x_name]
        if data.ndim == 2:
            data = data[np.newaxis, :, :]
    else:
        dimensions = [y_name, x_name]
    dset = _create_dataset(
        group=group,
        name=name,
        dimensions=dimensions,
        data=data,
        long_name=long_name,
        description=description,
        fillvalue=fillvalue,
        attrs=attrs,
    )
    dset.attrs["grid_mapping"] = grid_mapping_dset_name
    return dset


def _create_yx_arrays(
    gt: list[float], shape: tuple[int, int]
) -> tuple[np.ndarray, np.ndarray]:
    """Create the x and y coordinate datasets."""
    ysize, xsize = shape
    # Parse the geotransform
    x_origin, x_res, _, y_origin, _, y_res = gt

    # Make the x/y arrays
    # Note that these are the center of the pixels, whereas the GeoTransform
    # is the upper left corner of the top left pixel.
    y = np.arange(y_origin + y_res / 2, y_origin + y_res * ysize, y_res)
    x = np.arange(x_origin + x_res / 2, x_origin + x_res * xsize, x_res)
    return y, x


def _create_yx_dsets(
    group: h5netcdf.Group,
    gt: list[float],
    shape: tuple[int, int],
    include_time: bool = False,
    x_name: str = "x",
    y_name: str = "y",
) -> tuple[h5netcdf.Variable, h5netcdf.Variable]:
    """Create the y, x, and coordinate datasets."""
    y, x = _create_yx_arrays(gt, shape)

    if not group.dimensions:
        dims = {y_name: y.size, x_name: x.size}
        if include_time:
            dims["time"] = 1
        group.dimensions = dims

    # Create the x/y datasets
    y_ds = group.create_variable(y_name, (y_name,), data=y, dtype=float)
    x_ds = group.create_variable(x_name, (x_name,), data=x, dtype=float)

    for name, ds in zip([y_name, x_name], [y_ds, x_ds]):
        ds.attrs["standard_name"] = f"projection_{name}_coordinate"
        ds.attrs["long_name"] = f"{name.replace('_', ' ')} coordinate of projection"
        ds.attrs["units"] = "m"
    return y_ds, x_ds


def _create_time_dset(
    group: h5netcdf.Group,
    time: datetime.datetime,
    long_name: str = "time",
    variable_name: str = "time",
    dimension_name: str = "time",
) -> tuple[h5netcdf.Variable, h5netcdf.Variable]:
    """Create the time coordinate dataset."""
    times, calendar, units = _create_time_array([time])
    t_ds = group.create_variable(
        variable_name, (dimension_name,), data=times, dtype=float
    )
    t_ds.attrs["standard_name"] = "time"
    t_ds.attrs["long_name"] = long_name
    t_ds.attrs["calendar"] = calendar
    t_ds.attrs["units"] = units

    return t_ds


def _create_time_array(times: list[datetime.datetime]):
    """Set up the CF-compliant time array and dimension metadata.

    References
    ----------
    http://cfconventions.org/cf-conventions/cf-conventions.html#time-coordinate

    """
    # 'calendar': 'standard',
    # 'units': 'seconds since 2017-02-03 00:00:00.000000'
    # Create the time array
    since_time = times[0]
    time = np.array([(t - since_time).total_seconds() for t in times])
    calendar = "standard"
    units = f"seconds since {since_time.strftime(DATETIME_FORMAT)}"
    return time, calendar, units


def _create_grid_mapping(
    group, crs: pyproj.CRS, gt: list[float], name: str = GRID_MAPPING_DSET
) -> h5netcdf.Variable:
    """Set up the grid mapping variable."""
    # https://github.com/corteva/rioxarray/blob/21284f67db536d9c104aa872ab0bbc261259e59e/rioxarray/rioxarray.py#L34
    dset = group.create_variable(name, (), data=0, dtype=int)

    dset.attrs.update(crs.to_cf())
    # Also add the GeoTransform
    gt_string = " ".join([str(x) for x in gt])
    dset.attrs.update(
        {
            "GeoTransform": gt_string,
            "units": "unitless",
            "long_name": "Dummy variable with geo-referencing metadata in attributes",
        }
    )

    return dset


class CompressedSLCInfo(NamedTuple):
    """Data for creating one compressed SLC HDF5."""

    burst_id: str
    comp_slc_file: Path
    output_dir: Path
    opera_cslc_file: Path


def process_compressed_slc(info: CompressedSLCInfo) -> Path:
    """Make one compressed SLC output product."""
    burst_id, comp_slc_file, output_dir, opera_cslc_file = info
    date_str = format_dates(*get_dates(comp_slc_file.stem))
    name = COMPRESSED_SLC_TEMPLATE.format(burst_id=burst_id, date_str=date_str)
    outname = Path(output_dir) / name

    if outname.exists():
        logger.info(f"Skipping existing {outname}")

    crs = io.get_raster_crs(comp_slc_file)
    gt = io.get_raster_gt(comp_slc_file)
    data = io.load_gdal(comp_slc_file, band=1)
    # COMPASS used `truncate_mantissa` default, 10 bits
    round_mantissa(data, keep_bits=10)

    # Input metadata is stored within the GDAL "DOLPHIN" domain
    metadata_dict = io.get_raster_metadata(comp_slc_file, "DOLPHIN")
    attrs = {"units": "unitless"}
    attrs.update(metadata_dict)

    *parts, dset_name = OPERA_DATASET_NAME.split("/")
    dispersion_dset_name = "amplitude_dispersion"
    group_name = "/".join(parts)
    logger.info(f"Writing {outname}")
    with h5py.File(outname, "w") as hf:
        # add type to root for GDAL recognition of complex datasets in NetCDF
        ctype = h5py.h5t.py_create(np.complex64)
        ctype.commit(hf["/"].id, np.bytes_("complex64"))

    # COMPASS used "_coordinates" instead of "x"/"y"
    x_name, y_name = "x_coordinates", "y_coordinates"
    grid_mapping_dset_name = "projection"
    with h5netcdf.File(outname, mode="a", invalid_netcdf=True) as f:
        f.attrs.update(attrs)

        data_group = f.create_group(group_name)
        # COMPASS used "projection" instead of "spatial_ref"
        _create_grid_mapping(
            group=data_group, crs=crs, gt=gt, name=grid_mapping_dset_name
        )
        _create_yx_dsets(
            group=data_group,
            gt=gt,
            shape=data.shape,
            include_time=False,
            x_name=x_name,
            y_name=y_name,
        )
        _create_geo_dataset(
            group=data_group,
            name=dset_name,
            data=data,
            long_name="Compressed SLC",
            description="Compressed SLC product",
            fillvalue=np.nan + 0j,
            attrs=attrs,
            x_name=x_name,
            y_name=y_name,
            grid_mapping_dset_name=grid_mapping_dset_name,
        )
        del data

        # Add the amplitude dispersion
        amp_dispersion_data = io.load_gdal(comp_slc_file, band=2).real.astype("float32")
        round_mantissa(amp_dispersion_data, keep_bits=10)
        _create_geo_dataset(
            group=data_group,
            name=dispersion_dset_name,
            data=amp_dispersion_data,
            long_name="Amplitude Dispersion",
            description="Amplitude dispersion for the compressed SLC files.",
            fillvalue=np.nan,
            attrs={"units": "unitless"},
            x_name=x_name,
            y_name=y_name,
            grid_mapping_dset_name=grid_mapping_dset_name,
        )

    copy_cslc_metadata_to_compressed(opera_cslc_file, outname)

    return outname


def _copy_hdf5_dsets(
    source_file: Filename,
    dest_file: Filename,
    dsets_to_copy: Iterable[str],
    prepend_str: str = "",
    error_on_missing: bool = False,
) -> None:
    with h5py.File(source_file, "r") as src, h5py.File(dest_file, "a") as dst:
        for dset_path in dsets_to_copy:
            if dset_path not in src:
                msg = f"Dataset or group {dset_path} not found in {source_file}"
                if error_on_missing:
                    raise ValueError(msg)
                else:
                    logger.warning(msg)
                    continue

            # Create parent group if it doesn't exist
            out_group = str(Path(dset_path).parent)
            dst.require_group(out_group)

            # Remove existing dataset/group if it exists
            if dset_path in dst:
                del dst[dset_path]

            # Copy the dataset or group
            new_name = f"{prepend_str}{Path(dset_path).name}"
            src.copy(src[dset_path], dst[str(Path(dset_path).parent)], name=new_name)


def copy_cslc_metadata_to_compressed(
    opera_cslc_file: Filename, output_hdf5_file: Filename
) -> None:
    """Copy orbit and metadata datasets from the input CSLC file to the compressed SLC.

    Parameters
    ----------
    opera_cslc_file : Filename
        Path to the input CSLC file.
    output_hdf5_file : Filename
        Path to the output compressed SLC file.

    """
    dsets_to_copy = [
        "/metadata/orbit",  #          Group
        "/metadata/processing_information/input_burst_metadata/wavelength",
        "/metadata/processing_information/input_burst_metadata/platform_id",
        "/metadata/processing_information/input_burst_metadata/radar_center_frequency",
        "/metadata/processing_information/input_burst_metadata/ipf_version",
        "/metadata/processing_information/algorithms/COMPASS_version",
        "/metadata/processing_information/algorithms/ISCE3_version",
        "/metadata/processing_information/algorithms/s1_reader_version",
        "/identification/zero_doppler_end_time",
        "/identification/zero_doppler_start_time",
        "/identification/bounding_polygon",
        "/identification/mission_id",
        "/identification/instrument_name",
        "/identification/look_direction",
        "/identification/track_number",
        "/identification/orbit_pass_direction",
        "/identification/absolute_orbit_number",
    ]
    _copy_hdf5_dsets(
        source_file=opera_cslc_file,
        dest_file=output_hdf5_file,
        dsets_to_copy=dsets_to_copy,
    )
    logger.debug(f"Copied metadata from {opera_cslc_file} to {output_hdf5_file}")


def copy_cslc_metadata_to_displacement(
    reference_cslc_file: Filename,
    secondary_cslc_file: Filename,
    output_disp_file: Filename,
) -> None:
    """Copy metadata from input reference/secondary CSLC files to DISP output."""
    dsets_to_copy = [
        "/metadata/orbit",  #          Group
    ]
    for cslc_file, prepend_str in zip(
        [reference_cslc_file, secondary_cslc_file], ["reference_", "secondary_"]
    ):
        _copy_hdf5_dsets(
            source_file=cslc_file,
            dest_file=output_disp_file,
            dsets_to_copy=dsets_to_copy,
            prepend_str=prepend_str,
        )

    # Add ones which should be same for both ref/sec
    common_dsets = [
        "/identification/mission_id",
        "/identification/instrument_name",
        "/identification/look_direction",
        "/identification/track_number",
        "/identification/orbit_pass_direction",
        "/identification/absolute_orbit_number",
        "/metadata/processing_information/input_burst_metadata/platform_id",
        "/metadata/processing_information/input_burst_metadata/wavelength",
        "/metadata/processing_information/input_burst_metadata/radar_center_frequency",
        "/metadata/processing_information/algorithms/COMPASS_version",
        "/metadata/processing_information/algorithms/ISCE3_version",
        "/metadata/processing_information/algorithms/s1_reader_version",
    ]
    _copy_hdf5_dsets(
        source_file=reference_cslc_file,
        dest_file=output_disp_file,
        dsets_to_copy=common_dsets,
    )


def create_compressed_products(
    comp_slc_dict: Mapping[str, Sequence[Path]],
    output_dir: Filename,
    cslc_file_list: Sequence[Path],
    max_workers: int = 3,
) -> list[Path]:
    """Create all compressed SLC output products.

    Parameters
    ----------
    comp_slc_dict : dict[str, list[Path]]
        A dictionary mapping burst_id to lists of compressed SLC files.
    output_dir : Filename
        The directory to write the compressed SLC products to.
    cslc_file_list : Sequence[Path]
        Full set of input CSLCs used during processing.
        Used to pick out metadata corresponding to each compressed SLC's
        reference date.
    max_workers : int
        Number of parallel threads to use to create products.
        Default is 2.

    Returns
    -------
    list[Path]
        Paths to output compressed SLC files

    """
    compressed_slc_infos = []
    for burst_id, comp_slc_files in comp_slc_dict.items():
        for comp_slc_file in comp_slc_files:
            # Pick out the one that matches the current date/burst_id
            ref_date = get_dates(comp_slc_file)[0]
            valid_date_files = filter_by_date(cslc_file_list, [ref_date])
            matching_files = filter_by_burst_id(valid_date_files, burst_id)
            msg = (
                f"Found {len(matching_files)} matching CSLC files for"
                f" {burst_id} {ref_date}"
            )
            logger.info(msg)
            logger.info(matching_files)

            cur_opera_cslc = matching_files[-1]
            c = CompressedSLCInfo(burst_id, comp_slc_file, output_dir, cur_opera_cslc)
            compressed_slc_infos.append(c)

    executor_class = (
        ProcessPoolExecutor if max_workers > 1 else DummyProcessPoolExecutor
    )
    ctx = get_context("spawn")
    with executor_class(
        max_workers=max_workers,
        mp_context=ctx,
    ) as executor:
        results = list(executor.map(process_compressed_slc, compressed_slc_infos))

    logger.info("Finished creating all compressed SLC products.")
    return results


def extract_footprint(raster_path: Filename, simplify_tolerance: float = 0.01) -> str:
    """Extract a simplified footprint from a raster file.

    This function opens a raster file, extracts its footprint, simplifies it,
    and returns the a Polygon from the exterior ring as a WKT string.

    Parameters
    ----------
    raster_path : str
        Path to the input raster file.
    simplify_tolerance : float, optional
        Tolerance for simplification of the footprint geometry.
        Default is 0.01.

    Returns
    -------
    str
        WKT string representing the simplified exterior footprint
        in EPSG:4326 (lat/lon) coordinates.

    Notes
    -----
    This function uses GDAL to open the raster and extract the footprint,
    and Shapely to process the geometry.

    """
    from os import fspath

    import shapely
    from osgeo import gdal

    # Extract the footprint as WKT string (don't save)
    wkt = gdal.Footprint(
        None,
        fspath(raster_path),
        format="WKT",
        dstSRS="EPSG:4326",
        simplify=simplify_tolerance,
    )

    # Convert WKT to Shapely geometry, extract exterior, and convert back to Polygon WKT
    in_multi = shapely.from_wkt(wkt)

    # This may have holes; get the exterior
    # Largest polygon should be first in MultiPolygon returned by GDAL
    footprint = shapely.Polygon(in_multi.geoms[0].exterior)
    return footprint.wkt
