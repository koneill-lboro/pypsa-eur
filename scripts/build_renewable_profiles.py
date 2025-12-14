# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
"""
Calculates for each clustered region the (i) installable capacity (based on
land-use from :mod:`determine_availability_matrix`), (ii) the available
generation time series (based on weather data), and (iii) the average distance
from the node for onshore wind, AC-connected offshore wind, DC-connected
offshore wind and solar PV generators.

Script: build_renewable_profiles.py
Purpose: Generate the technical potential (p_nom_max) and hourly capacity
         factor profiles for renewable generators. This is the SOURCE of the
         data used in add_existing_baseyear.py for capacity distribution.

===============================================================================
DATA FLOW OVERVIEW
===============================================================================

Input Files:
    1. cutout (NetCDF via atlite): Weather data cutout
       - Variables: wind speed, solar irradiance, temperature
       - Resolution: Typically 0.3° × 0.3° grid (~30km)
       - Source: ERA5 reanalysis or other weather datasets
       - Time: Hourly data for simulation year(s)

    2. availability_matrix (NetCDF): Land availability per grid cell
       - Built by: determine_availability_matrix.py
       - Values: 0-1 fraction of cell available for technology
       - Accounts for: protected areas, settlements, slopes, etc.

    3. distance_regions (GeoPackage): Region geometries
       - For onshore: Regions are clustered bus zones
       - For offshore: Exclusive Economic Zones (EEZ)

    4. resource_regions (GeoPackage): Regions for resource class calculation

Output Files:
    - profile_{technology}.nc with structure:
      * profile: (year, bus, bin, time) - hourly capacity factors [0-1]
      * p_nom_max: (bus, bin) - installable capacity in MW
      * average_distance: (bus, bin) - distance to grid/shore in km

    - class_regions (GeoPackage): Geometry of resource class zones

===============================================================================
CRITICAL CALCULATION: INSTALLABLE CAPACITY (p_nom_max)
===============================================================================

The p_nom_max determines how much renewable capacity CAN BE installed at
each bus. This is used in:
- add_existing_baseyear.py for distributing existing capacity
- solve_network.py as upper bound for new capacity

FORMULA (line 285):
    p_nom_max = capacity_per_sqkm × availability × area

Where:
    - capacity_per_sqkm: From config (e.g., 5 MW/km² for solar utility)
    - availability: Fraction of land available (0-1)
    - area: Grid cell area in km²

⚠️ CRITICAL: This creates the TECHNICAL POTENTIAL that constrains all
subsequent capacity calculations. If p_nom_max is too low:
- Existing capacity distribution may exceed potential
- New investment options are artificially constrained

===============================================================================
CRITICAL CALCULATION: CAPACITY FACTOR PROFILES
===============================================================================

Capacity factor represents hourly generation per unit of installed capacity.

CALCULATION FLOW:

1. Weather Data → Raw Capacity Factor (line 184):
   func = getattr(cutout, resource.pop("method"))
   capacity_factor = correction_factor * func(capacity_factor=True, **resource)

   Where 'method' is typically:
   - 'pv': Solar irradiance → DC output using panel model
   - 'wind': Wind speed → power output using power curve

2. Layout Weighting (line 247):
   layout = capacity_factor * area * capacity_per_sqkm

   This determines how much capacity goes in each grid cell.
   MORE CAPACITY placed where capacity factor is HIGHER.

   ⚠️ This is the assumption that creates the capacity-factor-weighted
   distribution used in add_existing_baseyear.py

3. Aggregated Profile (lines 262-269):
   profile = func(matrix=matrix, layout=layout, per_unit=True, ...)

   The profile is capacity-weighted average of grid cell capacity factors
   within each region.

===============================================================================
CRITICAL CALCULATION: RESOURCE CLASSES (BINS)
===============================================================================

Resource classes divide capacity within each region by quality.

PURPOSE:
- Allows modeling of resource heterogeneity
- Better sites (higher CF) can be used first
- More accurate total system costs

CALCULATION (lines 211-217):
1. Compute capacity factor for each grid cell within region
2. Define bins based on CF range: bins = linspace(cf_min, cf_max, nbins+1)
3. Assign each cell to a bin based on its CF
4. Create separate p_nom_max and profile for each bin

EXAMPLE with nbins=3:
- Bin 0: Low CF cells (e.g., 0.10-0.15 for solar)
- Bin 1: Medium CF cells (e.g., 0.15-0.20 for solar)
- Bin 2: High CF cells (e.g., 0.20-0.25 for solar)

===============================================================================
CRITICAL CALCULATION: AVERAGE DISTANCE
===============================================================================

Distance affects grid connection costs for offshore wind.

CALCULATION (lines 294-302):
For each (bus, bin):
    distance = weighted_avg(grid_cell_distance, weight=layout)

Where:
- Onshore: Distance from cell to bus representative point
- Offshore: Distance from cell to shoreline

Used in prepare_sector_network.py to calculate:
    connection_cost = distance × cable_cost_per_km

===============================================================================
KEY PARAMETERS FROM CONFIG
===============================================================================

Located in config['renewable'][technology]:

1. capacity_per_sqkm:
   - Solar utility: ~5 MW/km² (ground-mounted)
   - Onshore wind: ~3-8 MW/km² (depending on turbine)
   - Offshore wind: ~3-10 MW/km²

2. correction_factor:
   - Multiplier for capacity factors (default 1.0)
   - Can account for technology improvements

3. resource_classes (nbins):
   - Number of quality bins (default 1)
   - More bins = more accurate but larger model

4. min_p_max_pu:
   - Minimum average CF to include region
   - Filters out very poor resource locations

5. clip_p_max_pu:
   - Minimum instantaneous CF to keep
   - Values below this set to 0

===============================================================================
DATA PROVENANCE FOR DEBUGGING
===============================================================================

To trace where p_nom_max comes from:
1. Check availability_matrix.nc - is land available?
2. Check cutout - are coordinates correct?
3. Check capacity_per_sqkm in config

To trace where profile (CF) comes from:
1. Check cutout weather data
2. Check resource parameters (turbine/panel model)
3. Check correction_factor

===============================================================================
INTERACTION WITH OTHER SCRIPTS
===============================================================================

This script provides data for:
- add_electricity.py: Creates generators with p_nom_max from profiles
- add_existing_baseyear.py: Distributes existing capacity using p_nom_max
- prepare_sector_network.py: Updates costs based on average_distance

===============================================================================

.. note:: Hydroelectric profiles are built in script :mod:`build_hydro_profiles`.

Outputs
-------

- ``resources/profile_{technology}.nc`` with the following structure

    ===================  ====================  =========================================================
    Field                Dimensions            Description
    ===================  ====================  =========================================================
    profile              year, bus, bin, time  the per unit hourly availability factors for each bus
    -------------------  --------------------  ---------------------------------------------------------
    p_nom_max            bus, bin              maximal installable capacity at the bus (in MW)
    -------------------  --------------------  ---------------------------------------------------------
    average_distance     bus, bin              average distance of units in the region to the
                                               grid bus for onshore technologies and to the shoreline
                                               for offshore technologies (in km)
    ===================  ====================  =========================================================

    - **profile**

    .. image:: img/profile_ts.png
        :scale: 33 %
        :align: center

    - **p_nom_max**

    .. image:: img/p_nom_max_hist.png
        :scale: 33 %
        :align: center

    - **average_distance**

    .. image:: img/distance_hist.png
        :scale: 33 %
        :align: center

Description
-----------

This script functions at two main spatial resolutions: the resolution of the
clustered network regions, and the resolution of the cutout grid cells for the
weather data. Typically the weather data grid is finer than the network regions,
so we have to work out the distribution of generators across the grid cells
within each region. This is done by taking account of a combination of the
available land at each grid cell (computed in
:mod:`determine_availability_matrix`) and the capacity factor there.

Based on the availability matrix, the script first computes how much of the
technology can be installed at each cutout grid cell. To compute the layout of
generators in each clustered region, the installable potential in each grid cell
is multiplied with the capacity factor at each grid cell. This is done since we
assume more generators are installed at cells with a higher capacity factor.

Based on the average capacity factor, the potentials are further divided into a
configurable number of resource classes (bins).

.. image:: img/offwinddc-gridcell.png
    :scale: 50 %
    :align: center

.. image:: img/offwindac-gridcell.png
    :scale: 50 %
    :align: center

.. image:: img/onwind-gridcell.png
    :scale: 50 %
    :align: center

.. image:: img/solar-gridcell.png
    :scale: 50 %
    :align: center

This layout is then used to compute the generation availability time series from
the weather data cutout from ``atlite``.

The maximal installable potential for the node (`p_nom_max`) is computed by
adding up the installable potentials of the individual grid cells.
"""

import logging
import time
from itertools import product

import geopandas as gpd
import numpy as np
import pandas as pd
import xarray as xr
from atlite.gis import ExclusionContainer
from dask.distributed import Client

from scripts._helpers import (
    configure_logging,
    get_snapshots,
    load_cutout,
    set_scenario_config,
)
from scripts.build_shapes import _simplify_polys

logger = logging.getLogger(__name__)


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake(
            "build_renewable_profiles", clusters=38, technology="offwind-ac"
        )
    configure_logging(snakemake)
    set_scenario_config(snakemake)

    nprocesses = int(snakemake.threads)
    noprogress = snakemake.config["run"].get("disable_progressbar", True)
    noprogress = noprogress or not snakemake.config["atlite"]["show_progress"]
    technology = snakemake.wildcards.technology
    params = snakemake.params.renewable[technology]
    resource = params["resource"]  # pv panel params / wind turbine params
    resource["show_progress"] = not noprogress

    tech = next(t for t in ["panel", "turbine"] if t in resource)
    models = resource[tech]
    if not isinstance(models, dict):
        models = {0: models}
    resource[tech] = models[next(iter(models))]

    correction_factor = params.get("correction_factor", 1.0)
    capacity_per_sqkm = params["capacity_per_sqkm"]

    if correction_factor != 1.0:
        logger.info(f"correction_factor is set as {correction_factor}")

    if nprocesses > 1:
        client = Client(n_workers=nprocesses, threads_per_worker=1)
    else:
        client = None

    sns = get_snapshots(snakemake.params.snapshots, snakemake.params.drop_leap_day)

    cutout = load_cutout(snakemake.input.cutout, time=sns)

    availability = xr.open_dataarray(snakemake.input.availability_matrix)

    regions = gpd.read_file(snakemake.input.distance_regions)
    # do not pull up, set_index does not work if geo dataframe is empty
    regions = regions.set_index("name").rename_axis("bus")
    if snakemake.wildcards.technology.startswith("offwind"):
        # for offshore regions, the shortest distance to the shoreline is used
        offshore_regions = availability.coords["bus"].values
        regions = regions.loc[offshore_regions]
        regions = regions.map(lambda g: _simplify_polys(g, minarea=1)).set_crs(
            regions.crs
        )
    else:
        # for onshore regions, the representative point of the region is used
        regions = regions.representative_point()
    regions = regions.geometry.to_crs(3035)
    buses = regions.index

    area = cutout.grid.to_crs(3035).area / 1e6
    area = xr.DataArray(
        area.values.reshape(cutout.shape), [cutout.coords["y"], cutout.coords["x"]]
    )

    func = getattr(cutout, resource.pop("method"))
    if client is not None:
        resource["dask_kwargs"] = {"scheduler": client}

    logger.info(
        f"Calculate average capacity factor per grid cell for technology {technology}..."
    )
    start = time.time()

    capacity_factor = correction_factor * func(capacity_factor=True, **resource)

    duration = time.time() - start
    logger.info(
        f"Completed average capacity factor calculation per grid cell for technology {technology} ({duration:2.2f}s)"
    )

    nbins = params.get("resource_classes", 1)
    logger.info(
        f"Create masks for {nbins} resource classes for technology {technology}..."
    )
    start = time.time()

    fn = snakemake.input.resource_regions
    resource_regions = gpd.read_file(fn).set_index("name").rename_axis("bus").geometry

    # indicator matrix for which cells touch which regions
    kwargs = dict(nprocesses=nprocesses, disable_progressbar=noprogress)
    I = cutout.availabilitymatrix(resource_regions, ExclusionContainer(), **kwargs)
    I = np.ceil(I)
    cf_by_bus = capacity_factor * I.where(I > 0)

    epsilon = 1e-3
    cf_min, cf_max = (
        cf_by_bus.min(dim=["x", "y"]) - epsilon,
        cf_by_bus.max(dim=["x", "y"]) + epsilon,
    )
    normed_bins = xr.DataArray(np.linspace(0, 1, nbins + 1), dims=["bin"])
    bins = cf_min + (cf_max - cf_min) * normed_bins

    cf_by_bus_bin = cf_by_bus.expand_dims(bin=range(nbins))
    lower_edges = bins[:, :-1]
    upper_edges = bins[:, 1:]
    class_masks = (cf_by_bus_bin >= lower_edges) & (cf_by_bus_bin < upper_edges)

    if nbins == 1:
        bus_bin_mi = pd.MultiIndex.from_product(
            [resource_regions.index, [0]], names=["bus", "bin"]
        )
        class_regions = resource_regions.set_axis(bus_bin_mi)
    else:
        grid = cutout.grid.set_index(["y", "x"])
        class_regions = {}
        for bus, bin_id in product(buses, range(nbins)):
            bus_bin_mask = (
                class_masks.sel(bus=bus, bin=bin_id)
                .stack(spatial=["y", "x"])
                .to_pandas()
            )
            grid_cells = grid.loc[bus_bin_mask]
            geometry = (
                grid_cells.intersection(resource_regions.loc[bus]).union_all().buffer(0)
            )
            class_regions[(bus, bin_id)] = geometry
        class_regions = gpd.GeoSeries(class_regions, crs=4326)
        class_regions.index.names = ["bus", "bin"]
    class_regions.to_file(snakemake.output.class_regions)

    duration = time.time() - start
    logger.info(
        f"Completed resource class calculation for technology {technology} ({duration:2.2f}s)"
    )

    layout = capacity_factor * area * capacity_per_sqkm

    profiles = []
    for year, model in models.items():
        logger.info(
            f"Calculate weighted capacity factor time series for model {model} for technology {technology}..."
        )
        start = time.time()

        resource[tech] = model

        matrix = (availability * class_masks).stack(
            bus_bin=["bus", "bin"], spatial=["y", "x"]
        )

        profile = func(
            matrix=matrix,
            layout=layout,
            index=matrix.indexes["bus_bin"],
            per_unit=True,
            return_capacity=False,
            **resource,
        )
        profile = profile.unstack("bus_bin")

        dim = {"year": [year]}
        profile = profile.expand_dims(dim)

        profiles.append(profile.rename("profile"))

        duration = time.time() - start
        logger.info(
            f"Completed weighted capacity factor time series calculation for model {model} for technology {technology} ({duration:2.2f}s)"
        )

    profiles = xr.merge(profiles)

    logger.info(f"Calculating maximal capacity per bus for technology {technology}")
    p_nom_max = capacity_per_sqkm * availability * class_masks @ area

    logger.info(f"Calculate average distances for technology {technology}.")
    layoutmatrix = (layout * availability * class_masks).stack(
        bus_bin=["bus", "bin"], spatial=["y", "x"]
    )

    coords = cutout.grid.representative_point().to_crs(3035)

    average_distance = []
    bus_bins = layoutmatrix.indexes["bus_bin"]
    for bus, bin in bus_bins:
        row = layoutmatrix.sel(bus=bus, bin=bin).data
        nz_b = row != 0
        row = row[nz_b]
        co = coords[nz_b]
        distances = co.distance(regions[bus]).div(1e3)  # km
        average_distance.append((distances * (row / row.sum())).sum())

    average_distance = xr.DataArray(average_distance, [bus_bins]).unstack("bus_bin")

    ds = xr.merge(
        [
            correction_factor * profiles,
            p_nom_max.rename("p_nom_max"),
            average_distance.rename("average_distance"),
        ]
    )
    # select only buses with some capacity and minimal capacity factor
    mean_profile = ds["profile"].mean("time").max(["year", "bin"])
    sum_potential = ds["p_nom_max"].sum("bin")

    ds = ds.sel(
        bus=(
            (mean_profile > params.get("min_p_max_pu", 0.0))
            & (sum_potential > params.get("min_p_nom_max", 0.0))
        )
    )

    if "clip_p_max_pu" in params:
        min_p_max_pu = params["clip_p_max_pu"]
        ds["profile"] = ds["profile"].where(ds["profile"] >= min_p_max_pu, 0)

    ds.to_netcdf(snakemake.output.profile)

    if client is not None:
        client.shutdown()
