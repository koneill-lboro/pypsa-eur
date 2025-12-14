# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
"""
Prepares brownfield data from previous planning horizon.

Script: add_brownfield.py
Purpose: Transfer optimized capacity from one planning horizon to the next in
         myopic optimization. This is the KEY script that enables multi-period
         investment planning with path dependency.

===============================================================================
DATA FLOW OVERVIEW
===============================================================================

Input Files:
    1. network (NetCDF): Fresh network for current planning horizon
       - Built by prepare_sector_network.py for the new horizon
       - Contains new investment options but NO previous capacity decisions
       - Has updated technology costs for the new planning year

    2. network_p (NetCDF): SOLVED network from PREVIOUS planning horizon
       - Contains optimized capacity decisions (p_nom_opt, e_nom_opt)
       - Has build_year and lifetime attributes set
       - Example: If solving 2040, this is the solved 2030 network

Output:
    - Combined network with:
      * Previous optimized capacity transferred as non-extendable
      * New investment options available for optimization
      * Grid expansion locked in from previous decisions
      * Asset retirement based on lifetime constraints

===============================================================================
CRITICAL CALCULATION: CAPACITY TRANSFER
===============================================================================

The add_brownfield() function performs the key capacity transfer:

    CORE LOGIC (lines 100-103):
        c.df[f"{attr}_nom"] = c.df[f"{attr}_nom_opt"]  # Optimized → Fixed
        c.df[f"{attr}_nom_extendable"] = False          # Lock it in

    This means:
    - p_nom_opt (optimized capacity) becomes p_nom (fixed capacity)
    - Asset can no longer expand in future horizons
    - Asset continues to operate until retirement

    COMPONENT-SPECIFIC HANDLING:
    - Generators: p_nom_opt → p_nom
    - Links: p_nom_opt → p_nom
    - Stores: e_nom_opt → e_nom (storage capacity)

===============================================================================
CRITICAL CALCULATION: ASSET RETIREMENT
===============================================================================

Assets are removed from future horizons based on lifetime:

    RETIREMENT CHECK (line 70):
        n_p.remove(c.name, c.df.index[c.df.build_year + c.df.lifetime <= year])

    Example:
    - Asset built in 2010 with 20-year lifetime
    - In 2030 horizon: 2010 + 20 = 2030, so asset IS retired (<=)
    - In 2025 horizon: 2010 + 20 = 2030 > 2025, so asset survives

    ⚠️ KEY DETAIL: The <= means assets retire AT END of their final year
    - A 2010 asset with 20-year life operates through 2029, not 2030

===============================================================================
CRITICAL CALCULATION: TRANSMISSION GRID EXPANSION
===============================================================================

Grid expansion is handled specially for lines and DC links:

    LINES (line 58):
        n.lines.s_nom_min = n_p.lines.s_nom_opt

    DC LINKS (lines 59-60):
        n.links.loc[dc_i, "p_nom_min"] = n_p.links.loc[dc_i, "p_nom_opt"]

    This sets the MINIMUM capacity to the optimized value, meaning:
    - Grid cannot contract (no decommissioning of transmission)
    - Grid can further expand if transmission expansion is allowed
    - Creates path-dependent transmission investment

===============================================================================
SPECIAL HANDLING: THRESHOLD FILTERING
===============================================================================

Small capacities are removed to improve solver performance (lines 92-98):

    if p_nom_opt < capacity_threshold:
        component is removed

    EXCEPTION - CHP Heat Links:
    CHP heat output is proportional to electric output via:
        threshold_heat = threshold × efficiency_electric × p_nom_ratio / efficiency_heat

    This ensures CHP electric and heat links are removed consistently.

===============================================================================
SPECIAL HANDLING: HYDROGEN PIPELINE RETROFIT
===============================================================================

For scenarios with H2 pipeline retrofitting (lines 114-156):

1. Gas pipelines can be converted to H2 pipelines
2. Already-retrofitted capacity is tracked across horizons
3. Remaining gas pipeline capacity decreases as more is retrofitted

    Formula:
        remaining_H2_retrofit_potential = original_potential - already_retrofitted
        remaining_gas_capacity = original_gas - CH4_per_H2 × already_retrofitted

    Where CH4_per_H2 accounts for different volumetric capacity of H2 vs CH4.

===============================================================================
SPECIAL HANDLING: RENEWABLE PROFILE UPDATES
===============================================================================

The adjust_renewable_profiles() function updates capacity factors for:
- Solar: May have degradation or technology improvement data by year
- Wind: May have updated wind resource data by year

    Selection logic (line 232-233):
        closest_year = max(y for y in ds.year.values if y <= year)

    Uses the most recent profile data available up to the planning year.

===============================================================================
SPECIAL HANDLING: HEAT PUMP EFFICIENCY UPDATES
===============================================================================

The update_heat_pump_efficiency() function (lines 246-293):

Heat pumps from previous years receive CURRENT year COP values because:
- Ambient temperatures may change (climate scenarios)
- District heating supply temperatures may decrease over time
- Ground source temperatures may be affected by widespread adoption

This means 2030-vintage heat pumps in a 2050 network use 2050 COPs.

===============================================================================
TRANSMISSION EXPANSION LIMIT CHECK
===============================================================================

The disable_grid_expansion_if_limit_hit() function:

If transmission expansion has reached its limit (from global constraints):
1. Check if current minimum capacity ≈ expansion limit
2. If limit reached:
   - Set s_nom/p_nom = s_nom_min/p_nom_min (lock in capacity)
   - Disable further expansion (extendable = False)
   - Remove the now-redundant global constraint

This prevents solver from wasting time on infeasible expansion.

===============================================================================
UNDERSTANDING THE MYOPIC WORKFLOW
===============================================================================

Complete myopic workflow for 2020→2030→2040:

1. YEAR 2020 (Base Year):
   - add_existing_baseyear.py: Add historical capacity
   - solve_network.py: Optimize for 2020
   - Output: solved_2020.nc with p_nom_opt set

2. YEAR 2030:
   - prepare_sector_network.py: Build fresh 2030 network
   - add_brownfield.py: Merge solved_2020.nc into 2030 network
     * 2020 optimized capacity → 2030 fixed capacity
     * 2020 assets with lifetime ending ≤ 2030 are retired
   - solve_network.py: Optimize for 2030
   - Output: solved_2030.nc

3. YEAR 2040:
   - prepare_sector_network.py: Build fresh 2040 network
   - add_brownfield.py: Merge solved_2030.nc into 2040 network
     * 2020 and 2030 assets transferred (if not retired)
     * 2020 assets may now retire (build_year + lifetime ≤ 2040)
   - solve_network.py: Optimize for 2040

===============================================================================
DEBUGGING GUIDE
===============================================================================

Common Issue: "Asset disappeared between horizons"
Cause: build_year + lifetime <= current_year (retirement)
Check: Compare asset lifetime with planning horizon gap
Fix: Extend lifetime in costs.csv if unrealistic

Common Issue: "Capacity too high in future year"
Cause: Small capacities not being filtered
Check: capacity_threshold in config
Fix: Adjust threshold or investigate why small assets persist

Common Issue: "Grid expansion not working"
Cause: Expansion limit already reached
Check: Logs for "Transmission expansion ... already reached"
Fix: Increase expansion limit or accept constraint

Common Issue: "CHP mismatch between horizons"
Cause: Electric/heat links filtered inconsistently
Check: Compare CHP electric and heat Link indices
Fix: Verify p_nom_ratio and efficiency values

===============================================================================
USE CASE CONSIDERATIONS
===============================================================================

This script is designed for:
✓ Myopic multi-period capacity expansion modeling
✓ Path-dependent investment decisions
✓ Asset lifetime tracking and retirement

This script assumes:
- Previous network was successfully solved (has *_nom_opt values)
- Component naming is consistent between horizons
- Time series indices are compatible

===============================================================================
"""

import logging

import numpy as np
import pandas as pd
import pypsa
import xarray as xr

from scripts._helpers import (
    configure_logging,
    get_snapshots,
    sanitize_custom_columns,
    set_scenario_config,
    update_config_from_wildcards,
)
from scripts.add_electricity import flatten, sanitize_carriers
from scripts.add_existing_baseyear import add_build_year_to_new_assets

logger = logging.getLogger(__name__)
idx = pd.IndexSlice


def add_brownfield(
    n,
    n_p,
    year,
    h2_retrofit=False,
    h2_retrofit_capacity_per_ch4=None,
    capacity_threshold=None,
):
    """
    Add brownfield capacity from previous network.

    This is the CORE function for myopic optimization. It transfers optimized
    capacity decisions from the previous planning horizon to the current one,
    handling asset retirement, capacity locking, and infrastructure constraints.

    Parameters
    ----------
    n : pypsa.Network
        Fresh network for current planning year (to be modified in-place)
        - Has new investment options with p_nom_extendable=True
        - Has updated costs for the planning year
        - Does NOT have previous capacity decisions

    n_p : pypsa.Network
        SOLVED network from PREVIOUS planning horizon
        - Has p_nom_opt/e_nom_opt from optimization
        - Has build_year and lifetime for all assets
        - Will be modified during processing (components removed)

    year : int
        Current planning year (e.g., 2030, 2040)
        Used for lifetime calculations and asset retirement

    h2_retrofit : bool, default False
        Whether hydrogen pipeline retrofitting is enabled
        If True: Gas pipeline capacity is reduced as H2 pipelines are built

    h2_retrofit_capacity_per_ch4 : float, optional
        Volumetric ratio of H2 to CH4 capacity in retrofitted pipelines
        Typical value: ~0.8-0.9 (H2 has lower volumetric energy density)
        Only used if h2_retrofit=True

    capacity_threshold : float, optional
        Minimum capacity (MW) to retain. Assets with p_nom_opt below this
        threshold are removed to improve solver performance.
        Typical value: 0.1 to 10 MW depending on model resolution

    Calculation Details
    -------------------

    1. TRANSMISSION GRID (Lines 57-60):
       The previous optimized transmission becomes the MINIMUM for future.

       n.lines.s_nom_min = n_p.lines.s_nom_opt

       This ensures:
       - No grid contraction (cannot decommission transmission)
       - Further expansion possible up to s_nom_max

    2. ASSET FILTERING (Lines 62-98):
       Three categories of assets are removed from n_p before transfer:

       a) Infinite-lifetime assets (line 67):
          - CO2 tracking, EU-wide aggregators
          - These already exist in fresh network n

       b) Retired assets (line 70):
          - build_year + lifetime <= year
          - Asset has reached end of life

       c) Small-capacity assets (lines 72-98):
          - p_nom_opt < capacity_threshold
          - Exception: CHP links use adjusted threshold

    3. CAPACITY TRANSFER (Lines 100-103):
       For each surviving Link/Generator/Store:

       c.df["p_nom"] = c.df["p_nom_opt"]     # Lock in optimized capacity
       c.df["p_nom_extendable"] = False      # No further expansion

       For Stores: uses e_nom and e_nom_opt instead.

    4. TIME SERIES TRANSFER (Lines 106-112):
       All time-dependent inputs are copied:
       - p_max_pu, p_min_pu for generators
       - efficiency time series for links
       - inflow for storage units

    5. H2 RETROFIT HANDLING (Lines 114-156):
       If h2_retrofit=True:

       a) Calculate already-retrofitted H2 pipeline capacity
       b) Reduce remaining retrofit potential:
          remaining = original_potential - already_retrofitted

       c) Reduce gas pipeline capacity proportionally:
          gas_remaining = original_gas - CH4_per_H2 × already_retrofitted

       Where CH4_per_H2 = 1 / h2_retrofit_capacity_per_ch4

    Example Walkthrough
    -------------------
    Previous horizon (2030) solved with:
    - Generator "DE1 solar-2030": p_nom_opt = 5000 MW
    - Link "DE1 CCGT-2010": p_nom_opt = 1000 MW, lifetime = 30 years

    Current horizon (2040):
    - Solar asset: Transferred with p_nom = 5000 MW, p_nom_extendable = False
    - CCGT asset: build_year + lifetime = 2010 + 30 = 2040 → RETIRED (<=2040)

    Debugging Tips
    --------------
    To trace capacity flow between horizons:
    1. Check n_p.generators.p_nom_opt before add_brownfield
    2. After call: Check n.generators.p_nom for transferred values
    3. Missing assets: Check lifetime constraint or threshold filtering

    To debug retirement:
    - Log: n_p.generators[["build_year", "lifetime"]]
    - Calculate: build_year + lifetime vs current year
    - Assets where sum <= year will be removed

    See Also
    --------
    add_existing_baseyear.py : Sets initial build_year and lifetime
    solve_network.py : Produces p_nom_opt values
    """
    logger.info(f"Preparing brownfield for the year {year}")

    # electric transmission grid set optimised capacities of previous as minimum
    n.lines.s_nom_min = n_p.lines.s_nom_opt
    dc_i = n.links[n.links.carrier == "DC"].index
    n.links.loc[dc_i, "p_nom_min"] = n_p.links.loc[dc_i, "p_nom_opt"]

    for c in n_p.iterate_components(["Link", "Generator", "Store"]):
        attr = "e" if c.name == "Store" else "p"

        # first, remove generators, links and stores that track
        # CO2 or global EU values since these are already in n
        n_p.remove(c.name, c.df.index[c.df.lifetime == np.inf])

        # remove assets whose build_year + lifetime <= year
        n_p.remove(c.name, c.df.index[c.df.build_year + c.df.lifetime <= year])

        # remove assets if their optimized nominal capacity is lower than a threshold
        # since CHP heat Link is proportional to CHP electric Link, make sure threshold is compatible
        chp_heat = c.df.index[
            (c.df[f"{attr}_nom_extendable"] & c.df.index.str.contains("urban central"))
            & c.df.index.str.contains("CHP")
            & c.df.index.str.contains("heat")
        ]

        if not chp_heat.empty:
            threshold_chp_heat = (
                capacity_threshold
                * c.df.efficiency[chp_heat.str.replace("heat", "electric")].values
                * c.df.p_nom_ratio[chp_heat.str.replace("heat", "electric")].values
                / c.df.efficiency[chp_heat].values
            )
            n_p.remove(
                c.name,
                chp_heat[c.df.loc[chp_heat, f"{attr}_nom_opt"] < threshold_chp_heat],
            )

        n_p.remove(
            c.name,
            c.df.index[
                (c.df[f"{attr}_nom_extendable"] & ~c.df.index.isin(chp_heat))
                & (c.df[f"{attr}_nom_opt"] < capacity_threshold)
            ],
        )

        # copy over assets but fix their capacity
        c.df[f"{attr}_nom"] = c.df[f"{attr}_nom_opt"]
        c.df[f"{attr}_nom_extendable"] = False

        n.add(c.name, c.df.index, **c.df)

        # copy time-dependent
        selection = n.component_attrs[c.name].type.str.contains(
            "series"
        ) & n.component_attrs[c.name].status.str.contains("Input")
        for tattr in n.component_attrs[c.name].index[selection]:
            # TODO: Needs to be rewritten to
            n._import_series_from_df(c.pnl[tattr], c.name, tattr)

    # deal with gas network
    if h2_retrofit:
        # subtract the already retrofitted from the maximum capacity
        h2_retrofitted_fixed_i = n.links[
            (n.links.carrier == "H2 pipeline retrofitted")
            & (n.links.build_year != year)
        ].index
        h2_retrofitted = n.links[
            (n.links.carrier == "H2 pipeline retrofitted")
            & (n.links.build_year == year)
        ].index

        # pipe capacity always set in prepare_sector_network to todays gas grid capacity * H2_per_CH4
        # and is therefore constant up to this point
        pipe_capacity = n.links.loc[h2_retrofitted, "p_nom_max"]
        # already retrofitted capacity from gas -> H2
        already_retrofitted = (
            n.links.loc[h2_retrofitted_fixed_i, "p_nom"]
            .rename(lambda x: x.split("-2")[0] + f"-{year}")
            .groupby(level=0)
            .sum()
        )
        remaining_capacity = pipe_capacity - already_retrofitted.reindex(
            index=pipe_capacity.index
        ).fillna(0)
        n.links.loc[h2_retrofitted, "p_nom_max"] = remaining_capacity

        # reduce gas network capacity
        gas_pipes_i = n.links[n.links.carrier == "gas pipeline"].index
        if not gas_pipes_i.empty:
            # subtract the already retrofitted from today's gas grid capacity
            pipe_capacity = n.links.loc[gas_pipes_i, "p_nom"]
            fr = "H2 pipeline retrofitted"
            to = "gas pipeline"
            CH4_per_H2 = 1 / h2_retrofit_capacity_per_ch4
            already_retrofitted.index = already_retrofitted.index.str.replace(fr, to)
            remaining_capacity = (
                pipe_capacity
                - CH4_per_H2
                * already_retrofitted.reindex(index=pipe_capacity.index).fillna(0)
            )
            n.links.loc[gas_pipes_i, "p_nom"] = remaining_capacity
            n.links.loc[gas_pipes_i, "p_nom_max"] = remaining_capacity


def disable_grid_expansion_if_limit_hit(n):
    """
    Check if transmission expansion limit is already reached; then turn off.

    In particular, this function checks if the total transmission
    capital cost or volume implied by s_nom_min and p_nom_min are
    numerically close to the respective global limit set in
    n.global_constraints. If so, the nominal capacities are set to the
    minimum and extendable is turned off; the corresponding global
    constraint is then dropped.
    """
    types = {"expansion_cost": "capital_cost", "volume_expansion": "length"}
    for limit_type in types:
        glcs = n.global_constraints.query(f"type == 'transmission_{limit_type}_limit'")

        for name, glc in glcs.iterrows():
            total_expansion = (
                (
                    n.lines.query("s_nom_extendable")
                    .eval(f"s_nom_min * {types[limit_type]}")
                    .sum()
                )
                + (
                    n.links.query("carrier == 'DC' and p_nom_extendable")
                    .eval(f"p_nom_min * {types[limit_type]}")
                    .sum()
                )
            ).sum()

            # Allow small numerical differences
            if np.abs(glc.constant - total_expansion) / glc.constant < 1e-6:
                logger.info(
                    f"Transmission expansion {limit_type} is already reached, disabling expansion and limit"
                )
                extendable_acs = n.lines.query("s_nom_extendable").index
                n.lines.loc[extendable_acs, "s_nom_extendable"] = False
                n.lines.loc[extendable_acs, "s_nom"] = n.lines.loc[
                    extendable_acs, "s_nom_min"
                ]

                extendable_dcs = n.links.query(
                    "carrier == 'DC' and p_nom_extendable"
                ).index
                n.links.loc[extendable_dcs, "p_nom_extendable"] = False
                n.links.loc[extendable_dcs, "p_nom"] = n.links.loc[
                    extendable_dcs, "p_nom_min"
                ]

                n.global_constraints.drop(name, inplace=True)


def adjust_renewable_profiles(n, input_profiles, params, year):
    """
    Adjusts renewable profiles according to the renewable technology specified,
    using the latest year below or equal to the selected year.
    """

    # temporal clustering
    dr = get_snapshots(params["snapshots"], params["drop_leap_day"])
    snapshotmaps = (
        pd.Series(dr, index=dr).where(lambda x: x.isin(n.snapshots), pd.NA).ffill()
    )

    for carrier in params["carriers"]:
        if carrier == "hydro":
            continue

        with xr.open_dataset(getattr(input_profiles, "profile_" + carrier)) as ds:
            if ds.indexes["bus"].empty or "year" not in ds.indexes:
                continue

            ds = ds.stack(bus_bin=["bus", "bin"])

            closest_year = max(
                (y for y in ds.year.values if y <= year), default=min(ds.year.values)
            )

            p_max_pu = ds["profile"].sel(year=closest_year).to_pandas()
            p_max_pu.columns = p_max_pu.columns.map(flatten) + f" {carrier}"

            # temporal_clustering
            p_max_pu = p_max_pu.groupby(snapshotmaps).mean()

            # replace renewable time series
            n.generators_t.p_max_pu.loc[:, p_max_pu.columns] = p_max_pu


def update_heat_pump_efficiency(n: pypsa.Network, n_p: pypsa.Network, year: int):
    """
    Update the efficiency of heat pumps from previous years to current year
    (e.g. 2030 heat pumps receive 2040 heat pump COPs in 2030).

    Parameters
    ----------
    n : pypsa.Network
        The original network.
    n_p : pypsa.Network
        The network with the updated parameters.
    year : int
        The year for which the efficiency is being updated.

    Returns
    -------
    None
        This function updates the efficiency in place and does not return a value.
    """

    # get names of heat pumps in previous iteration that cannot be replaced by direct utilisation in this iteration
    heat_pump_idx_previous_iteration = n_p.links.index[
        n_p.links.index.str.contains("heat pump")
        & n_p.links.index.str[:-4].isin(
            n.links_t.efficiency.columns.str.rstrip(  # sources that can be directly used are no longer represented by heat pumps in the dynamic efficiency dataframe
                str(year)
            )
        )
    ]
    # construct names of same-technology heat pumps in the current iteration
    corresponding_idx_this_iteration = heat_pump_idx_previous_iteration.str[:-4] + str(
        year
    )
    # update efficiency of heat pumps in previous iteration in-place to efficiency in this iteration
    n_p.links_t["efficiency"].loc[:, heat_pump_idx_previous_iteration] = (
        n.links_t["efficiency"].loc[:, corresponding_idx_this_iteration].values
    )

    # Change efficiency2 for heat pumps that use an explicitly modelled heat source
    previous_iteration_columns = heat_pump_idx_previous_iteration.intersection(
        n_p.links_t["efficiency2"].columns
    )
    current_iteration_columns = corresponding_idx_this_iteration.intersection(
        n.links_t["efficiency2"].columns
    )
    n_p.links_t["efficiency2"].loc[:, previous_iteration_columns] = (
        n.links_t["efficiency2"].loc[:, current_iteration_columns].values
    )


def update_dynamic_ptes_capacity(
    n: pypsa.Network, n_p: pypsa.Network, year: int
) -> None:
    """
    Updates dynamic pit storage capacity based on district heating temperature changes.

    Parameters
    ----------
    n : pypsa.Network
        Original network.
    n_p : pypsa.Network
        Network with updated parameters.
    year : int
        Target year for capacity update.

    Returns
    -------
    None
        Updates capacity in-place.
    """
    # pit storages in previous iteration
    dynamic_ptes_idx_previous_iteration = n_p.stores.index[
        n_p.stores.index.str.contains("water pits")
    ]
    # construct names of same-technology dynamic pit storage in the current iteration
    corresponding_idx_this_iteration = dynamic_ptes_idx_previous_iteration.str[
        :-4
    ] + str(year)
    # update pit storage capacity in previous iteration in-place to capacity in this iteration
    n_p.stores_t.e_max_pu[dynamic_ptes_idx_previous_iteration] = n.stores_t.e_max_pu[
        corresponding_idx_this_iteration
    ].values


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake(
            "add_brownfield",
            clusters="39",
            opts="",
            sector_opts="",
            planning_horizons=2050,
        )

    configure_logging(snakemake)  # pylint: disable=E0606
    set_scenario_config(snakemake)

    update_config_from_wildcards(snakemake.config, snakemake.wildcards)

    logger.info(f"Preparing brownfield from the file {snakemake.input.network_p}")

    year = int(snakemake.wildcards.planning_horizons)

    n = pypsa.Network(snakemake.input.network)

    adjust_renewable_profiles(n, snakemake.input, snakemake.params, year)

    add_build_year_to_new_assets(n, year)

    n_p = pypsa.Network(snakemake.input.network_p)

    update_heat_pump_efficiency(n, n_p, year)

    if snakemake.params.tes and snakemake.params.dynamic_ptes_capacity:
        update_dynamic_ptes_capacity(n, n_p, year)

    add_brownfield(
        n,
        n_p,
        year,
        h2_retrofit=snakemake.params.H2_retrofit,
        h2_retrofit_capacity_per_ch4=snakemake.params.H2_retrofit_capacity_per_CH4,
        capacity_threshold=snakemake.params.threshold_capacity,
    )

    disable_grid_expansion_if_limit_hit(n)

    n.meta = dict(snakemake.config, **dict(wildcards=dict(snakemake.wildcards)))

    sanitize_custom_columns(n)
    sanitize_carriers(n, snakemake.config)
    n.export_to_netcdf(snakemake.output[0])
