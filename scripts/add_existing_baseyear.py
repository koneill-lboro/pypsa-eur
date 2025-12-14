# SPDX-FileCopyrightText: Contributors to PyPSA-Eur <https://github.com/pypsa/pypsa-eur>
#
# SPDX-License-Identifier: MIT
"""
Adds existing power and heat generation capacities for initial planning
horizon.

Script: add_existing_baseyear.py
Purpose: Initialize the base year network with historical/existing capacity for
         myopic optimization scenarios. This script is CRITICAL for brownfield
         modeling as it establishes the starting point for capacity expansion.

===============================================================================
DATA FLOW OVERVIEW
===============================================================================

Input Files:
    1. network (NetCDF): Pre-built sector network from prepare_sector_network.py
       - Contains topology, buses, and new investment options
       - Generators have p_nom_max (technical potential) but no existing capacity

    2. powerplants.csv: Power plant database (from powerplantmatching/OPSD)
       - Columns: Name, Fueltype, Technology, Capacity, DateIn, DateOut, Country, bus
       - Source: Open Power System Data + manual additions
       - Contains individual plant-level data for conventional generators

    3. costs.csv: Technology cost assumptions
       - Contains lifetime, efficiency, capital_cost for each technology
       - Used to calculate remaining lifetime of existing assets

    4. cop_profiles.nc: Heat pump coefficient of performance time series
       - Spatially and temporally varying COP values
       - Used for existing heat pump efficiency

    5. existing_heating_distribution.csv: Existing heating technology mix
       - Columns: (heat_system, technology) × nodes
       - Source: Derived from national heating statistics
       - Contains thermal capacity (MW_th) per technology per node

    6. heating_efficiencies.csv: Country-specific boiler efficiencies
       - Used to convert thermal capacity to fuel input capacity (p_nom)

Output:
    - Modified network (NetCDF) with existing capacities added as:
      * Generators with p_nom set (for renewables)
      * Links with p_nom set (for conventional plants and heating)
      * Build years assigned for lifetime tracking

===============================================================================
CRITICAL CALCULATION: RENEWABLE CAPACITY DISTRIBUTION
===============================================================================

The add_existing_renewables() function distributes NATIONAL renewable capacity
across NODES using a CAPACITY-FACTOR-WEIGHTED approach:

    Formula: zone_capacity[zone] = national_total × (p_nom_max[zone] / Σ p_nom_max)

    Where:
    - national_total: Total installed capacity from IRENA database (MW)
    - p_nom_max[zone]: Technical potential at zone (from land availability)

    DATA SOURCE for national_total:
    - IRENA STAT database via powerplantmatching library
    - Aggregated by country and year (yearly capacity additions since 2000)

    ⚠️ CRITICAL ASSUMPTION:
    This distributes capacity proportional to GENERATION POTENTIAL, not actual
    historical installation patterns. This means:
    - High-resource zones get more capacity than low-resource zones
    - Actual spatial distribution may differ significantly from model
    - May cause p_nom > p_nom_max in zones with historically high deployment

    WHY THIS APPROACH:
    - Actual sub-national installation data often unavailable
    - PyPSA-Eur is designed for scenario analysis, not historical validation
    - Simplifies data requirements for multi-country studies

    ALTERNATIVE APPROACH (for historical validation):
    - Use actual zonal capacity from grid registries (e.g., REPD for UK)
    - Modify existing_capacities data source in config

===============================================================================
CRITICAL CALCULATION: CONVENTIONAL POWER PLANT MAPPING
===============================================================================

The add_power_capacities_installed_before_baseyear() function:

1. Loads individual power plants from powerplants.csv
2. Groups by (grouping_year, Fueltype, bus) with capacity aggregated
3. Creates network components with appropriate build_year for lifetime tracking

    Grouping Years Logic:
    - Plants grouped into discrete vintage years (e.g., [1980, 1985, 1990, ...])
    - Remaining lifetime = DateOut - grouping_year + 1
    - Allows tracking of asset retirement through planning horizons

    ⚠️ KEY BEHAVIORS:
    - Plants with DateOut < baseyear are excluded (already retired)
    - Plants with DateIn > max(grouping_years) are dropped with WARNING
    - Lifetime is aggregated as MEAN within each group

    VALIDATION CHECK (lines 414-425):
    If p_nom_min > p_nom_max (existing exceeds potential):
    - WARNING is logged
    - p_nom_max is adjusted upward to accommodate existing capacity
    This can occur when:
    - Technical potential calculations are conservative
    - Historical spatial data differs from model distribution

===============================================================================
CRITICAL CALCULATION: HEATING CAPACITY INSTALLATION
===============================================================================

The add_heating_capacities_installed_before_baseyear() function:

    Installation Timeline Assumption:
    - Existing heating capacity assumed to be installed LINEARLY over time
    - Distributed across grouping_years based on interval duration
    - ratio = years_in_interval / total_years

    Formula: capacity_in_year[y] = total_existing × (interval_years / sum(interval_years))

    ⚠️ ASSUMPTIONS:
    - Linear historical installation rate (not exponential growth)
    - All heating types installed at same rate
    - Uniform age distribution within intervals

    Capacity Conversion:
    - Input is THERMAL capacity (MW_th)
    - Stored as FUEL INPUT capacity (p_nom = MW_th / efficiency)
    - This is standard PyPSA convention for Links

===============================================================================
USE CASE LIMITATIONS
===============================================================================

This script is designed for:
✓ Myopic optimization with brownfield constraints
✓ Scenario analysis with approximate historical starting point
✓ Multi-country European studies

This script may NOT be suitable for:
✗ Exact historical validation against zonal data
✗ Studies requiring precise spatial distribution of existing capacity
✗ Detailed plant-level dispatch modeling

For strict historical validation, consider:
- Replacing renewable distribution with actual zonal data
- Adding plant-level constraints if specific plants are critical

===============================================================================
DEBUGGING GUIDE
===============================================================================

Common Issue: "Base year capacity exceeds technical potential"
Cause: add_existing_renewables distributes more to zone than p_nom_max allows
Check: Compare IRENA national total vs. sum of p_nom_max across zones
Fix: Either increase p_nom_max or use alternative capacity data source

Common Issue: "Missing power plants"
Cause: DateIn > max(grouping_years)
Check: Warning in logs about dropped assets
Fix: Extend grouping_years_power in config

Common Issue: "Heating capacity mismatch"
Cause: Efficiency conversion or missing nodes
Check: existing_heating_distribution.csv node names vs network buses
Fix: Ensure node naming consistency

===============================================================================
"""

import logging
import re
from types import SimpleNamespace

import country_converter as coco
import numpy as np
import pandas as pd
import powerplantmatching as pm
import pypsa
import xarray as xr

from scripts._helpers import (
    configure_logging,
    load_costs,
    sanitize_custom_columns,
    set_scenario_config,
    update_config_from_wildcards,
)
from scripts.add_electricity import sanitize_carriers
from scripts.build_energy_totals import cartesian
from scripts.definitions.heat_system import HeatSystem
from scripts.prepare_sector_network import cluster_heat_buses, define_spatial

logger = logging.getLogger(__name__)
cc = coco.CountryConverter()
idx = pd.IndexSlice
spatial = SimpleNamespace()


def add_build_year_to_new_assets(n: pypsa.Network, baseyear: int) -> None:
    """
    Add build year to new assets in the network.

    Parameters
    ----------
    n : pypsa.Network
        Network to modify
    baseyear : int
        Year in which optimized assets are built
    """
    # Give assets with lifetimes and no build year the build year baseyear
    for c in n.iterate_components(["Link", "Generator", "Store"]):
        assets = c.df.index[(c.df.lifetime != np.inf) & (c.df.build_year == 0)]
        c.df.loc[assets, "build_year"] = baseyear

        # add -baseyear to name
        rename = pd.Series(c.df.index, c.df.index)
        rename[assets] += f"-{str(baseyear)}"
        c.df.rename(index=rename, inplace=True)

        # rename time-dependent
        selection = n.component_attrs[c.name].type.str.contains(
            "series"
        ) & n.component_attrs[c.name].status.str.contains("Input")
        for attr in n.component_attrs[c.name].index[selection]:
            c.pnl[attr] = c.pnl[attr].rename(columns=rename)


def add_existing_renewables(
    n: pypsa.Network,
    costs: pd.DataFrame,
    df_agg: pd.DataFrame,
    countries: list[str],
    renewable_carriers: list[str],
) -> None:
    """
    Add existing renewable capacities to conventional power plant data.

    This function distributes NATIONAL renewable capacity across NETWORK NODES
    using a proportional allocation based on technical potential (p_nom_max).

    Parameters
    ----------
    df_agg : pd.DataFrame
        DataFrame containing conventional power plant data, modified in-place
        to include renewable capacity entries
    costs : pd.DataFrame
        Technology cost data with 'lifetime' column indexed by technology
    n : pypsa.Network
        Network containing topology and generator data
    countries : list
        List of country codes (ISO 2-letter) to consider
    renewable_carriers : list
        List of renewable carriers in the network (e.g., ['solar', 'onwind', 'offwind-ac'])

    Returns
    -------
    None
        Modifies df_agg in-place by adding rows for renewable generators

    Data Flow
    ---------
    1. IRENA Data Retrieval (lines 161-166):
       - Source: powerplantmatching.data.IRENASTAT()
       - Returns: Historical capacity by (Technology, Country, Year)
       - Unit: MW
       - Coverage: Annual data from 2000 onwards

    2. Technology Mapping:
       tech_map = {"solar": "PV", "onwind": "Onshore", "offwind-ac": "Offshore"}
       - Maps PyPSA carrier names to IRENA technology categories
       - IRENA uses partial string matching (e.g., "PV" matches "Solar photovoltaic")

    3. Yearly Capacity Calculation (lines 177-180):
       - Takes cumulative capacity and computes YEARLY ADDITIONS
       - df.diff(axis=1) gives new installations each year
       - .clip(lower=0) handles any data anomalies (capacity shouldn't decrease)

    4. Spatial Distribution (lines 182-191):
       Formula: zone_cap = national_total × (p_nom_max[zone] / Σ p_nom_max)

       Where fraction is computed as:
           fraction = group.p_nom_max / group.p_nom_max.sum()

       The cartesian() function creates (year × zone) matrix of capacities.

    Critical Assumptions
    --------------------
    1. POTENTIAL-BASED DISTRIBUTION:
       - Capacity allocated proportional to technical potential (p_nom_max)
       - NOT based on actual historical installation locations
       - High-CF zones get proportionally more capacity

    2. UNIFORM COUNTRY DISTRIBUTION:
       - All zones in a country share the same national total
       - No sub-national allocation data used

    3. IRENA DATA ACCURACY:
       - Assumes IRENA national totals are accurate
       - Any data gaps filled with zeros

    Example Calculation
    -------------------
    For Germany (DE) with 50 GW solar in 2020:
    - Zone DE1: p_nom_max = 10 GW → gets 10/100 × 50 = 5 GW
    - Zone DE2: p_nom_max = 30 GW → gets 30/100 × 50 = 15 GW
    - Zone DE3: p_nom_max = 60 GW → gets 60/100 × 50 = 30 GW
    (where total potential = 100 GW)

    Known Limitations
    -----------------
    - May allocate more capacity to a zone than actually installed there
    - Offshore wind only uses "Offshore" category (no AC/DC split in IRENA)
    - Solar-rooftop not separately tracked (included in PV)

    See Also
    --------
    build_renewable_profiles.py : Generates p_nom_max from land availability
    powerplantmatching : Library providing IRENA data access
    """
    tech_map = {"solar": "PV", "onwind": "Onshore", "offwind-ac": "Offshore"}

    irena = pm.data.IRENASTAT().powerplant.convert_country_to_alpha2()
    irena = irena.query("Country in @countries")
    irena = irena.groupby(["Technology", "Country", "Year"]).Capacity.sum()

    irena = irena.unstack().reset_index()

    for carrier, tech in tech_map.items():
        if carrier not in renewable_carriers:
            continue
        df = (
            irena[irena.Technology.str.contains(tech)]
            .drop(columns=["Technology"])
            .set_index("Country")
        )
        df.columns = df.columns.astype(int)

        # calculate yearly differences
        df.insert(loc=0, value=0.0, column="1999")
        df = df.diff(axis=1).drop("1999", axis=1).clip(lower=0)

        # distribute capacities among generators potential (p_nom_max)
        gen_i = n.generators.query("carrier == @carrier").index
        carrier_gens = n.generators.loc[gen_i]
        res_capacities = []
        for country, group in carrier_gens.groupby(
            carrier_gens.bus.map(n.buses.country)
        ):
            fraction = group.p_nom_max / group.p_nom_max.sum()
            res_capacities.append(cartesian(df.loc[country], fraction))
        res_capacities = pd.concat(res_capacities, axis=1).T

        for year in res_capacities.columns:
            for gen in res_capacities.index:
                bus_bin = re.sub(f" {carrier}.*", "", gen)
                bus, bin_id = bus_bin.rsplit(" ", maxsplit=1)
                name = f"{bus_bin} {carrier}-{year}"
                capacity = res_capacities.loc[gen, year]
                if capacity > 0.0:
                    cost_key = carrier.split("-", maxsplit=1)[0]
                    df_agg.at[name, "Fueltype"] = carrier
                    df_agg.at[name, "Capacity"] = capacity
                    df_agg.at[name, "DateIn"] = year
                    df_agg.at[name, "lifetime"] = costs.at[cost_key, "lifetime"]
                    df_agg.at[name, "DateOut"] = (
                        year + costs.at[cost_key, "lifetime"] - 1
                    )
                    df_agg.at[name, "bus"] = bus
                    df_agg.at[name, "resource_class"] = bin_id

    df_agg["resource_class"] = df_agg["resource_class"].fillna(0)


def add_power_capacities_installed_before_baseyear(
    n: pypsa.Network,
    costs: pd.DataFrame,
    grouping_years: list[int],
    baseyear: int,
    powerplants_file: str,
    countries: list[str],
    capacity_threshold: float,
    lifetime_values: dict[str, float],
    renewable_carriers: list[str],
) -> None:
    """
    Add power generation capacities installed before base year.

    This function processes the power plant database to add existing conventional
    and renewable generators/links to the network. It is the primary function for
    initializing brownfield capacity in myopic optimization.

    Parameters
    ----------
    n : pypsa.Network
        Network to modify (in-place)
    costs : pd.DataFrame
        Technology costs indexed by technology name, with columns:
        'lifetime', 'efficiency', 'capital_cost', 'VOM', 'CO2 intensity'
    grouping_years : list
        Discrete years for grouping plant vintages, e.g., [1980, 1990, 2000, 2010, 2020]
        Plants are assigned to the nearest grouping year <= DateIn
    baseyear : int
        Base year for analysis (e.g., 2020, 2030). Plants with DateOut < baseyear excluded
    powerplants_file : str
        Path to powerplants CSV file (from build_powerplants rule)
    countries : list
        List of ISO 2-letter country codes to include
    capacity_threshold : float
        Minimum capacity (MW) to include. Smaller plants are dropped.
    lifetime_values : dict
        Default values for missing data, must contain 'lifetime' key
    renewable_carriers : list
        List of renewable carriers to process (e.g., ['solar', 'onwind', 'offwind-ac'])

    Data Flow
    ---------
    1. Load Power Plant Database:
       - Source: powerplants.csv (from build_powerplants.py using powerplantmatching)
       - Original sources: OPSD, GEO, ENTSOE, national registries

    2. Fuel Type Processing:
       Conventional fuels are remapped:
           "Hard Coal" → "coal"
           "Lignite" → "lignite"
           "Nuclear" → "nuclear"
           "Oil" → "oil"
           "OCGT" → "OCGT"
           "CCGT" → "CCGT"
           "Natural Gas" → Technology column (OCGT or CCGT)
           "Bioenergy" → "urban central solid biomass CHP"

       Dropped fuel types (handled separately or excluded):
           Hydro, Wind, Solar, Geothermal, Waste, Other

    3. Year Grouping Logic:
       - Each plant assigned to grouping_year = max(gy for gy in grouping_years if gy <= DateIn)
       - Remaining lifetime = DateOut - grouping_year + 1
       - This allows cohort-based retirement tracking across planning horizons

    4. Renewable Capacity Addition:
       - Calls add_existing_renewables() to distribute IRENA national totals
       - Creates synthetic plants for each (year, zone) combination

    5. Network Component Creation:
       - RENEWABLES: Added as Generators with build_year set
       - CONVENTIONAL: Added as Links (fuel bus → electricity bus → CO2 bus)
         * p_nom = capacity / efficiency (fuel input, not electrical output)
         * efficiency = electrical conversion efficiency
         * efficiency2 = CO2 intensity of fuel

    Grouping Year Example
    ---------------------
    With grouping_years = [1980, 1990, 2000, 2010, 2020]:
    - Plant built in 1985 → grouping_year = 1980
    - Plant built in 2003 → grouping_year = 2000
    - Plant built in 2022 → DROPPED (> max grouping year) with WARNING

    Capacity Aggregation
    --------------------
    Plants are aggregated by (grouping_year, Fueltype, bus):
    - Capacity: SUM
    - Lifetime: MEAN (across plants in same group)

    This simplifies the network while maintaining age-based retirement.

    Validation Check
    ----------------
    After adding renewables, the function checks if p_nom_min > p_nom_max.
    If existing capacity exceeds technical potential:
    - Logs WARNING with affected generators
    - Adjusts p_nom_max upward to accommodate existing capacity

    This is a CRITICAL check for debugging capacity distribution issues.

    Component Types Created
    -----------------------
    1. Renewables (Generator):
       - Carriers: solar, onwind, offwind-ac
       - Has build_year, lifetime from costs
       - Attached to bus (AC node)

    2. Fossil Fuels (Link):
       - Carriers: coal, lignite, nuclear, oil, OCGT, CCGT
       - bus0 = fuel bus (e.g., "EU coal")
       - bus1 = AC node
       - bus2 = "co2 atmosphere"
       - p_nom = capacity / efficiency (fuel input)

    3. Biomass CHP (Link):
       - bus0 = biomass bus
       - bus1 = AC node
       - bus2 = urban central heat bus (if district heating exists)

    Known Issues
    ------------
    1. Plants with DateIn > max(grouping_years) are silently dropped
       → Check logs for "newer_assets" warning
    2. Offshore wind uses generic "Offshore" category from IRENA
       → No distinction between AC and DC connection
    3. Mean lifetime aggregation may not represent actual plant distribution

    See Also
    --------
    add_brownfield.py : Uses build_year/lifetime for inter-horizon transfers
    build_powerplants.py : Creates the input powerplants.csv
    """
    logger.debug(f"Adding power capacities installed before {baseyear}")

    df_agg = pd.read_csv(powerplants_file, index_col=0)

    rename_fuel = {
        "Hard Coal": "coal",
        "Lignite": "lignite",
        "Nuclear": "nuclear",
        "Oil": "oil",
        "OCGT": "OCGT",
        "CCGT": "CCGT",
        "Bioenergy": "urban central solid biomass CHP",
    }

    # Replace Fueltype "Natural Gas" with the respective technology (OCGT or CCGT)
    df_agg.loc[df_agg["Fueltype"] == "Natural Gas", "Fueltype"] = df_agg.loc[
        df_agg["Fueltype"] == "Natural Gas", "Technology"
    ]

    fueltype_to_drop = [
        "Hydro",
        "Wind",
        "Solar",
        "Geothermal",
        "Waste",
        "Other",
        "CCGT, Thermal",
    ]

    technology_to_drop = ["Pv", "Storage Technologies"]

    # drop unused fueltypes and technologies
    df_agg.drop(df_agg.index[df_agg.Fueltype.isin(fueltype_to_drop)], inplace=True)
    df_agg.drop(df_agg.index[df_agg.Technology.isin(technology_to_drop)], inplace=True)
    df_agg.Fueltype = df_agg.Fueltype.map(rename_fuel)

    # Intermediate fix for DateIn & DateOut
    # Fill missing DateIn
    biomass_i = df_agg.loc[df_agg.Fueltype == "urban central solid biomass CHP"].index
    mean = df_agg.loc[biomass_i, "DateIn"].mean()
    df_agg.loc[biomass_i, "DateIn"] = df_agg.loc[biomass_i, "DateIn"].fillna(int(mean))
    # Fill missing DateOut
    dateout = df_agg.loc[biomass_i, "DateIn"] + lifetime_values["lifetime"]
    df_agg.loc[biomass_i, "DateOut"] = df_agg.loc[biomass_i, "DateOut"].fillna(dateout)

    # include renewables in df_agg
    add_existing_renewables(
        df_agg=df_agg,
        costs=costs,
        n=n,
        countries=countries,
        renewable_carriers=renewable_carriers,
    )
    # drop assets which are already phased out / decommissioned
    phased_out = df_agg[df_agg["DateOut"] < baseyear].index
    df_agg.drop(phased_out, inplace=True)

    newer_assets = (df_agg.DateIn > max(grouping_years)).sum()
    if newer_assets:
        logger.warning(
            f"There are {newer_assets} assets with build year "
            f"after last power grouping year {max(grouping_years)}. "
            "These assets are dropped and not considered."
            "Consider to redefine the grouping years to keep them."
        )
        to_drop = df_agg[df_agg.DateIn > max(grouping_years)].index
        df_agg.drop(to_drop, inplace=True)

    df_agg["grouping_year"] = np.take(
        grouping_years, np.digitize(df_agg.DateIn, grouping_years, right=True)
    )

    # calculate (adjusted) remaining lifetime before phase-out (+1 because assuming
    # phase out date at the end of the year)
    df_agg["lifetime"] = df_agg.DateOut - df_agg["grouping_year"] + 1

    df = df_agg.pivot_table(
        index=["grouping_year", "Fueltype", "resource_class"],
        columns="bus",
        values="Capacity",
        aggfunc="sum",
    )

    lifetime = df_agg.pivot_table(
        index=["grouping_year", "Fueltype", "resource_class"],
        columns="bus",
        values="lifetime",
        aggfunc="mean",  # currently taken mean for clustering lifetimes
    )

    carrier = {
        "OCGT": "gas",
        "CCGT": "gas",
        "coal": "coal",
        "oil": "oil",
        "lignite": "lignite",
        "nuclear": "uranium",
        "urban central solid biomass CHP": "biomass",
    }

    for grouping_year, generator, resource_class in df.index:
        # capacity is the capacity in MW at each node for this
        capacity = df.loc[grouping_year, generator, resource_class]
        capacity = capacity[~capacity.isna()]
        capacity = capacity[capacity > capacity_threshold]
        suffix = "-ac" if generator == "offwind" else ""
        name_suffix = f" {generator}{suffix}-{grouping_year}"
        asset_i = capacity.index + name_suffix
        if generator in ["solar", "onwind", "offwind-ac"]:
            asset_i = capacity.index + " " + resource_class + name_suffix
            name_suffix = " " + resource_class + name_suffix
            cost_key = generator.split("-")[0]
            # to consider electricity grid connection costs or a split between
            # solar utility and rooftop as well, rather take cost assumptions
            # from existing network than from the cost database
            capital_cost = n.generators.loc[
                n.generators.carrier == generator + suffix, "capital_cost"
            ].mean()
            marginal_cost = n.generators.loc[
                n.generators.carrier == generator + suffix, "marginal_cost"
            ].mean()
            # check if assets are already in network (e.g. for 2020)
            already_build = n.generators.index.intersection(asset_i)
            new_build = asset_i.difference(n.generators.index)

            # this is for the year 2020
            if not already_build.empty:
                n.generators.loc[already_build, "p_nom"] = n.generators.loc[
                    already_build, "p_nom_min"
                ] = capacity.loc[already_build.str.replace(name_suffix, "")].values
            new_capacity = capacity.loc[new_build.str.replace(name_suffix, "")]

            name_suffix_by = f" {resource_class} {generator}{suffix}-{baseyear}"
            p_max_pu = n.generators_t.p_max_pu[capacity.index + name_suffix_by]

            if not new_build.empty:
                n.add(
                    "Generator",
                    new_capacity.index,
                    suffix=name_suffix,
                    bus=new_capacity.index,
                    carrier=generator,
                    p_nom=new_capacity,
                    marginal_cost=marginal_cost,
                    capital_cost=capital_cost,
                    efficiency=costs.at[cost_key, "efficiency"],
                    p_max_pu=p_max_pu.rename(columns=n.generators.bus),
                    build_year=grouping_year,
                    lifetime=costs.at[cost_key, "lifetime"],
                )

        else:
            bus0 = vars(spatial)[carrier[generator]].nodes
            if "EU" not in vars(spatial)[carrier[generator]].locations:
                bus0 = bus0.intersection(capacity.index + " " + carrier[generator])

            # check for missing bus
            missing_bus = pd.Index(bus0).difference(n.buses.index)
            if not missing_bus.empty:
                logger.info(f"add buses {bus0}")
                n.add(
                    "Bus",
                    bus0,
                    carrier=generator,
                    location=vars(spatial)[carrier[generator]].locations,
                    unit="MWh_el",
                )

            already_build = n.links.index.intersection(asset_i)
            new_build = asset_i.difference(n.links.index)
            lifetime_assets = lifetime.loc[
                grouping_year, generator, resource_class
            ].dropna()

            # this is for the year 2020
            if not already_build.empty:
                n.links.loc[already_build, "p_nom_min"] = capacity.loc[
                    already_build.str.replace(name_suffix, "")
                ].values

            if not new_build.empty:
                new_capacity = capacity.loc[new_build.str.replace(name_suffix, "")]

                if generator != "urban central solid biomass CHP":
                    n.add(
                        "Link",
                        new_capacity.index,
                        suffix=name_suffix,
                        bus0=bus0,
                        bus1=new_capacity.index,
                        bus2="co2 atmosphere",
                        carrier=generator,
                        marginal_cost=costs.at[generator, "efficiency"]
                        * costs.at[generator, "VOM"],  # NB: VOM is per MWel
                        capital_cost=costs.at[generator, "efficiency"]
                        * costs.at[
                            generator, "capital_cost"
                        ],  # NB: fixed cost is per MWel
                        p_nom=new_capacity / costs.at[generator, "efficiency"],
                        efficiency=costs.at[generator, "efficiency"],
                        efficiency2=costs.at[carrier[generator], "CO2 intensity"],
                        build_year=grouping_year,
                        lifetime=lifetime_assets.loc[new_capacity.index],
                    )
                else:
                    key = "central solid biomass CHP"
                    central_heat = n.buses.query(
                        "carrier == 'urban central heat'"
                    ).location.unique()
                    heat_buses = new_capacity.index.map(
                        lambda i: i + " urban central heat" if i in central_heat else ""
                    )

                    n.add(
                        "Link",
                        new_capacity.index,
                        suffix=name_suffix,
                        bus0=spatial.biomass.df.loc[new_capacity.index]["nodes"].values,
                        bus1=new_capacity.index,
                        bus2=heat_buses,
                        carrier=generator,
                        p_nom=new_capacity / costs.at[key, "efficiency"],
                        capital_cost=costs.at[key, "capital_cost"]
                        * costs.at[key, "efficiency"],
                        marginal_cost=costs.at[key, "VOM"],
                        efficiency=costs.at[key, "efficiency"],
                        build_year=grouping_year,
                        efficiency2=costs.at[key, "efficiency-heat"],
                        lifetime=lifetime_assets.loc[new_capacity.index],
                    )
        # check if existing capacities are larger than technical potential
        existing_large = n.generators[
            n.generators["p_nom_min"] > n.generators["p_nom_max"]
        ].index
        if len(existing_large):
            logger.warning(
                f"Existing capacities larger than technical potential for {existing_large},\
                           adjust technical potential to existing capacities"
            )
            n.generators.loc[existing_large, "p_nom_max"] = n.generators.loc[
                existing_large, "p_nom_min"
            ]


def get_efficiency(
    heat_system: HeatSystem,
    carrier: str,
    nodes: pd.Index,
    efficiencies: dict[str, float],
    costs: pd.DataFrame,
) -> pd.Series | float:
    """
    Computes the heating system efficiency based on the sector and carrier
    type.

    Parameters
    ----------
    heat_system : object
    carrier : str
        The type of fuel or energy carrier (e.g., 'gas', 'oil').
    nodes : pandas.Series
        A pandas Series containing node information used to match the heating efficiency data.
    efficiencies : dict
        A dictionary containing efficiency values for different carriers and sectors.
    costs : pandas.DataFrame
        A DataFrame containing boiler cost and efficiency data for different heating systems.

    Returns
    -------
    efficiency : pandas.Series or float
        A pandas Series mapping the efficiencies based on nodes for residential and services sectors, or a single
        efficiency value for other heating systems (e.g., urban central).

    Notes
    -----
    - For residential and services sectors, efficiency is mapped based on the nodes.
    - For other sectors, the default boiler efficiency is retrieved from the `costs` database.
    """

    if heat_system.value == "urban central":
        boiler_costs_name = getattr(heat_system, f"{carrier}_boiler_costs_name")
        efficiency = costs.at[boiler_costs_name, "efficiency"]
    elif heat_system.sector.value == "residential":
        key = f"{carrier} residential space efficiency"
        efficiency = nodes.str[:2].map(efficiencies[key])
    elif heat_system.sector.value == "services":
        key = f"{carrier} services space efficiency"
        efficiency = nodes.str[:2].map(efficiencies[key])
    else:
        raise ValueError(f"Heat system {heat_system} not defined.")

    return efficiency


def add_heating_capacities_installed_before_baseyear(
    n: pypsa.Network,
    costs: pd.DataFrame,
    baseyear: int,
    grouping_years: list[int],
    existing_capacities: pd.DataFrame,
    heat_pump_cop: xr.DataArray,
    heat_pump_source_types: dict[str, list[str]],
    efficiency_file: str,
    use_time_dependent_cop: bool,
    default_lifetime: int,
    energy_totals_year: int,
    capacity_threshold: float,
    use_electricity_distribution_grid: bool,
) -> None:
    """
    Add heating capacities installed before base year.

    This function initializes the heating sector with existing capacity for heat pumps,
    resistive heaters, gas/oil/biomass boilers distributed across heat system types.

    Parameters
    ----------
    n : pypsa.Network
        Network to modify (in-place)
    costs : pd.DataFrame
        Technology costs indexed by technology name
    baseyear : int
        Base year for analysis (e.g., 2020)
    grouping_years : list
        Discrete years for grouping heating system vintages
        Example: [1980, 1990, 2000, 2010, 2020]
    existing_capacities : pd.DataFrame
        Existing heating capacity distribution with:
        - Index: node names (e.g., "DE1 0", "FR2 0")
        - Columns: MultiIndex of (heat_system, technology)
        - Values: Thermal capacity in MW_th
        - Heat systems: "urban central", "urban decentral", "rural"
        - Technologies: "air heat pump", "ground heat pump", "gas boiler", etc.
    heat_pump_cop : xr.DataArray
        Heat pump coefficients of performance
        - Dimensions: (time, heat_system, heat_source, name)
        - Values: COP ranging typically from 2.0 to 5.0
    heat_pump_source_types : dict
        Heat pump sources by system type, e.g.:
        {"central": ["air"], "decentral": ["air", "ground"]}
    efficiency_file : str
        Path to heating_efficiencies.csv with country-specific boiler efficiencies
    use_time_dependent_cop : bool
        If True: Use hourly COP profiles (temperature-dependent)
        If False: Use fixed average COP from costs database
    default_lifetime : int
        Default lifetime for heating systems in years (e.g., 25)
    energy_totals_year : int
        Year for efficiency data lookup (e.g., 2019)
    capacity_threshold : float
        Minimum capacity (MW) to include in network
    use_electricity_distribution_grid : bool
        If True: Heat pumps connect to "low voltage" buses
        If False: Heat pumps connect directly to main buses

    Data Flow
    ---------
    1. Existing Capacity Source:
       - File: existing_heating_distribution.csv
       - Built by: build_existing_heating_distribution.py
       - Original data: Eurostat, national heating surveys

    2. Installation Timeline Assumption:
       Existing capacity is assumed to be installed LINEARLY over time,
       distributed across grouping_years proportional to interval duration.

       Formula:
           ratio[year] = interval_years[year] / total_covered_years

       Example with grouping_years = [1990, 2000, 2010] and baseyear = 2020:
       - 1990 interval: 1990 to 1999 = 10 years
       - 2000 interval: 2000 to 2009 = 10 years
       - 2010 interval: 2010 to 2019 = 10 years
       - Total = 30 years
       - Each interval gets 1/3 of capacity

    3. Capacity Conversion (Thermal to Fuel Input):
       PyPSA Links store p_nom as FUEL INPUT, not thermal output.

       For boilers: p_nom = thermal_capacity / boiler_efficiency
       For heat pumps: Uses COP-adjusted efficiency

    4. Heat System Types:
       - "urban central": District heating networks
       - "urban decentral": Individual heating in urban areas
       - "rural": Individual heating in rural areas

    Technologies Added
    ------------------
    1. Heat Pumps (Link):
       - Carriers: "{heat_system} {source} heat pump"
       - bus0: heat bus (output)
       - bus1: electricity bus (input, note: reversed from typical Link)
       - efficiency: 1/COP (heat input per electricity)
       - p_max_pu: 0 (cannot export heat to electricity)
       - p_min_pu: -efficiency (can consume electricity to produce heat)

    2. Resistive Heaters (Link):
       - Carrier: "{heat_system} resistive heater"
       - efficiency: ~0.99 (near-perfect conversion)
       - Simple electric heating without COP benefit

    3. Gas Boilers (Link):
       - bus0: gas bus ("EU gas" or node-specific)
       - bus1: heat bus
       - bus2: "co2 atmosphere" (for emissions tracking)
       - efficiency: Country-specific from efficiency_file

    4. Oil Boilers (Link):
       - Similar structure to gas boilers
       - bus0: oil bus

    5. Biomass Boilers (Link):
       - bus0: biomass bus
       - Only added if capacity > 0 in existing_capacities

    Efficiency Handling
    -------------------
    Efficiencies come from two sources:
    1. Costs database (costs.csv): Default technology efficiencies
    2. Country-specific (heating_efficiencies.csv): Regional variations

    For residential/services: Uses country-specific efficiency
    For urban central: Uses costs database efficiency

    Cleanup Operations
    ------------------
    After adding all components, the function removes:
    - Links with p_nom = NaN (missing nodes)
    - Links with p_nom < capacity_threshold (negligible capacity)

    Known Assumptions
    -----------------
    1. Linear historical installation rate (not exponential growth)
    2. All heating technologies assumed to have same age distribution
    3. No differentiation between heat pump vintages (all get current COP)
    4. Building stock changes not modeled (existing capacity stays in same locations)

    Debugging Tips
    --------------
    - Check existing_capacities.csv for correct node names
    - Verify heat system types match network bus carriers
    - NaN in p_nom usually indicates missing bus/node mismatch

    See Also
    --------
    build_existing_heating_distribution.py : Creates existing_capacities input
    prepare_sector_network.py : Creates heat buses and initial network structure
    """
    logger.debug(f"Adding heating capacities installed before {baseyear}")

    # Load heating efficiencies
    heating_efficiencies = pd.read_csv(efficiency_file, index_col=[1, 0]).loc[
        energy_totals_year
    ]

    ratios = []
    valid_grouping_years = []

    for heat_system in existing_capacities.columns.get_level_values(0).unique():
        heat_system = HeatSystem(heat_system)

        nodes = pd.Index(
            n.buses.location[n.buses.index.str.contains(f"{heat_system} heat")]
        )

        if (
            not heat_system == HeatSystem.URBAN_CENTRAL
        ) and use_electricity_distribution_grid:
            nodes_elec = nodes + " low voltage"
        else:
            nodes_elec = nodes

            too_large_grouping_years = [
                gy for gy in grouping_years if gy >= int(baseyear)
            ]
            if too_large_grouping_years:
                logger.warning(
                    f"Grouping years >= baseyear are ignored. Dropping {too_large_grouping_years}."
                )
            valid_grouping_years = pd.Series(
                [
                    int(grouping_year)
                    for grouping_year in grouping_years
                    if int(grouping_year) + default_lifetime > int(baseyear)
                    and int(grouping_year) < int(baseyear)
                ]
            )

            assert valid_grouping_years.is_monotonic_increasing

            if len(valid_grouping_years) == 0:
                logger.warning(
                    f"No valid grouping years found for {heat_system}. "
                    "No existing capacities will be added."
                )
                ratios = []
            else:
                # get number of years of each interval
                _years = valid_grouping_years.diff()
                # Fill NA from .diff() with value for the first interval
                _years[0] = valid_grouping_years[0] - baseyear + default_lifetime
                # Installation is assumed to be linear for the past
                ratios = _years / _years.sum()

        for ratio, grouping_year in zip(ratios, valid_grouping_years):
            # Add heat pumps
            for heat_source in heat_pump_source_types[heat_system.system_type.value]:
                costs_name = heat_system.heat_pump_costs_name(heat_source)

                efficiency = (
                    heat_pump_cop.sel(
                        heat_system=heat_system.system_type.value,
                        heat_source=heat_source,
                        name=nodes,
                    )
                    .to_pandas()
                    .reindex(index=n.snapshots)
                    if use_time_dependent_cop
                    else costs.at[costs_name, "efficiency"]
                )

                n.add(
                    "Link",
                    nodes,
                    suffix=f" {heat_system} {heat_source} heat pump-{grouping_year}",
                    bus0=nodes + " " + heat_system.value + " heat",
                    bus1=nodes_elec,
                    carrier=f"{heat_system} {heat_source} heat pump",
                    efficiency=1 / efficiency.clip(lower=0.001),
                    capital_cost=costs.at[costs_name, "capital_cost"],
                    p_nom=existing_capacities.loc[
                        nodes, (heat_system.value, f"{heat_source} heat pump")
                    ]
                    * ratio,
                    p_max_pu=0,
                    p_min_pu=-1 * efficiency / efficiency.clip(lower=0.001),
                    build_year=int(grouping_year),
                    lifetime=costs.at[costs_name, "lifetime"],
                )

            # add resistive heater, gas boilers and oil boilers
            n.add(
                "Link",
                nodes,
                suffix=f" {heat_system} resistive heater-{grouping_year}",
                bus0=nodes_elec,
                bus1=nodes + " " + heat_system.value + " heat",
                carrier=heat_system.value + " resistive heater",
                efficiency=costs.at[
                    heat_system.resistive_heater_costs_name, "efficiency"
                ],
                capital_cost=(
                    costs.at[heat_system.resistive_heater_costs_name, "efficiency"]
                    * costs.at[heat_system.resistive_heater_costs_name, "capital_cost"]
                ),
                p_nom=(
                    existing_capacities.loc[
                        nodes, (heat_system.value, "resistive heater")
                    ]
                    * ratio
                    / costs.at[heat_system.resistive_heater_costs_name, "efficiency"]
                ),
                build_year=int(grouping_year),
                lifetime=costs.at[heat_system.resistive_heater_costs_name, "lifetime"],
            )

            efficiency = get_efficiency(
                heat_system, "gas", nodes, heating_efficiencies, costs
            )

            n.add(
                "Link",
                nodes,
                suffix=f" {heat_system} gas boiler-{grouping_year}",
                bus0="EU gas" if "EU gas" in spatial.gas.nodes else nodes + " gas",
                bus1=nodes + " " + heat_system.value + " heat",
                bus2="co2 atmosphere",
                carrier=heat_system.value + " gas boiler",
                efficiency=efficiency,
                efficiency2=costs.at["gas", "CO2 intensity"],
                capital_cost=(
                    costs.at[heat_system.gas_boiler_costs_name, "efficiency"]
                    * costs.at[heat_system.gas_boiler_costs_name, "capital_cost"]
                ),
                p_nom=(
                    existing_capacities.loc[nodes, (heat_system.value, "gas boiler")]
                    * ratio
                    / costs.at[heat_system.gas_boiler_costs_name, "efficiency"]
                ),
                build_year=int(grouping_year),
                lifetime=costs.at[heat_system.gas_boiler_costs_name, "lifetime"],
            )

            efficiency = get_efficiency(
                heat_system, "oil", nodes, heating_efficiencies, costs
            )

            n.add(
                "Link",
                nodes,
                suffix=f" {heat_system} oil boiler-{grouping_year}",
                bus0=spatial.oil.nodes,
                bus1=nodes + " " + heat_system.value + " heat",
                bus2="co2 atmosphere",
                carrier=heat_system.value + " oil boiler",
                efficiency=efficiency,
                efficiency2=costs.at["oil", "CO2 intensity"],
                capital_cost=costs.at[heat_system.oil_boiler_costs_name, "efficiency"]
                * costs.at[heat_system.oil_boiler_costs_name, "capital_cost"],
                p_nom=(
                    existing_capacities.loc[nodes, (heat_system.value, "oil boiler")]
                    * ratio
                    / costs.at[heat_system.oil_boiler_costs_name, "efficiency"]
                ),
                build_year=int(grouping_year),
                lifetime=costs.at[
                    f"{heat_system.central_or_decentral} gas boiler", "lifetime"
                ],
            )

            efficiency = get_efficiency(
                heat_system, "biomass", nodes, heating_efficiencies, costs
            )

            # prevents redundant addition of urban central biomass boiler which tends to crash
            if (
                existing_capacities.loc[
                    nodes, (heat_system.value, "biomass boiler")
                ].sum()
                > 0
            ):
                n.add(
                    "Link",
                    nodes,
                    suffix=f" {heat_system} biomass boiler-{grouping_year}",
                    bus0=spatial.biomass.nodes,
                    bus1=nodes + " " + heat_system.value + " heat",
                    carrier=heat_system.value + " biomass boiler",
                    efficiency=efficiency,
                    capital_cost=efficiency
                    * costs.at["biomass boiler", "capital_cost"],
                    p_nom=(
                        existing_capacities.loc[
                            nodes, (heat_system.value, "biomass boiler")
                        ]
                        * ratio
                        / efficiency
                    ),
                    build_year=int(grouping_year),
                    lifetime=costs.at["biomass boiler", "lifetime"],
                )

            # delete links with p_nom=nan corresponding to extra nodes in country
            n.remove(
                "Link",
                [
                    index
                    for index in n.links.index.to_list()
                    if str(grouping_year) in index and np.isnan(n.links.p_nom[index])
                ],
            )

            # delete links with capacities below threshold
            n.remove(
                "Link",
                [
                    index
                    for index in n.links.index.to_list()
                    if str(grouping_year) in index
                    and n.links.p_nom[index] < capacity_threshold
                ],
            )


if __name__ == "__main__":
    if "snakemake" not in globals():
        from scripts._helpers import mock_snakemake

        snakemake = mock_snakemake(
            "add_existing_baseyear",
            configfiles="config/test/config.myopic.yaml",
            clusters="5",
            opts="",
            sector_opts="",
            planning_horizons=2030,
        )

    configure_logging(snakemake)  # pylint: disable=E0606
    set_scenario_config(snakemake)

    update_config_from_wildcards(snakemake.config, snakemake.wildcards)

    options = snakemake.params.sector

    renewable_carriers = snakemake.params.carriers

    baseyear = snakemake.params.baseyear

    n = pypsa.Network(snakemake.input.network)

    # define spatial resolution of carriers
    spatial = define_spatial(n.buses[n.buses.carrier == "AC"].index, options)
    add_build_year_to_new_assets(n, baseyear)

    costs = load_costs(snakemake.input.costs)

    grouping_years_power = snakemake.params.existing_capacities["grouping_years_power"]
    grouping_years_heat = snakemake.params.existing_capacities["grouping_years_heat"]
    add_power_capacities_installed_before_baseyear(
        n=n,
        costs=costs,
        grouping_years=grouping_years_power,
        baseyear=baseyear,
        powerplants_file=snakemake.input.powerplants,
        countries=snakemake.config["countries"],
        capacity_threshold=snakemake.params.existing_capacities["threshold_capacity"],
        lifetime_values=snakemake.params.costs["fill_values"],
        renewable_carriers=renewable_carriers,
    )

    if options["heating"]:
        # one could use baseyear here instead (but dangerous if no data)
        fn = snakemake.input.heating_efficiencies
        year = int(snakemake.params["energy_totals_year"])
        heating_efficiencies = pd.read_csv(fn, index_col=[1, 0]).loc[year]

        add_heating_capacities_installed_before_baseyear(
            n=n,
            costs=costs,
            baseyear=baseyear,
            grouping_years=grouping_years_heat,
            heat_pump_cop=xr.open_dataarray(snakemake.input.cop_profiles),
            use_time_dependent_cop=options["time_dep_hp_cop"],
            default_lifetime=snakemake.params.existing_capacities[
                "default_heating_lifetime"
            ],
            existing_capacities=pd.read_csv(
                snakemake.input.existing_heating_distribution,
                header=[0, 1],
                index_col=0,
            ),
            heat_pump_source_types=snakemake.params.heat_pump_sources,
            efficiency_file=snakemake.input.heating_efficiencies,
            energy_totals_year=snakemake.params["energy_totals_year"],
            capacity_threshold=snakemake.params.existing_capacities[
                "threshold_capacity"
            ],
            use_electricity_distribution_grid=options["electricity_distribution_grid"],
        )

    # Set defaults for missing missing values

    if options.get("cluster_heat_buses", False):
        cluster_heat_buses(n)

    n.meta = dict(snakemake.config, **dict(wildcards=dict(snakemake.wildcards)))

    sanitize_custom_columns(n)
    sanitize_carriers(n, snakemake.config)
    n.export_to_netcdf(snakemake.output[0])
