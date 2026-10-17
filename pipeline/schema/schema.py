from pydantic import BaseModel, Field
from typing import List, Optional, Literal

from core.utils import CanonicalMemberSize, EWPProductType



# =====================================================================
#  ENUMS & SHARED TYPES
# =====================================================================

SupportMaterial = Literal[
    "DF-L", "SP", "SPF", "HF",
    "concrete", "steel",
    "LVL", "PSL", "LSL", "glulam"
]

class LumberSpec(BaseModel):
    species: Literal["DF-L", "SP", "SPF", "HF", "LVL", "PSL", "LSL", "glulam"] | None = None
    grade: Literal["SS", "No.1", "No.2", "No.3", "Stud", "1.55E", "2.0E", "2.2E", "24F-V4"] | None = None
    species_grade_note: str

class EWPSpec(BaseModel):
    product_type: EWPProductType | None = None
    Fb_psi: float | None = None
    Fv_psi: float | None = None
    Fc_perp_psi: float | None = None
    E_psi: float | None = None
    E_min_psi: float | None = None

class MemberSize(BaseModel):
    """Parsed member size. AI extracts size_label string, calc engine parses to dimensions."""
    size_label: CanonicalMemberSize | None = None  # EXTRACT: canonical "2x10" or "1.75x11.875"
    nominal_width_in: int | None = None  # SKIP_EXTRACTION: parsed from size_label by calc engine
    nominal_depth_in: int | None = None  # SKIP_EXTRACTION: parsed from size_label by calc engine


# =====================================================================
#  PROJECT CONTEXT  (AI-extracted from plans — feeds the lateral calc)
# =====================================================================

class BuildingGeometry(BaseModel):
    """Overall building envelope from arch/structural plans. The calc engine
    uses this for ASCE 7 wind/seismic force generation (lateral_calc)."""
    length_ft: float | None = None               # EXTRACT: plan dimension parallel to ridge (long axis)
    width_ft: float | None = None                # EXTRACT: plan dimension perpendicular to ridge (short axis)
    eave_height_ft: float | None = None          # EXTRACT: from elevations/sections
    ridge_height_ft: float | None = None         # EXTRACT: from elevations/sections
    mean_roof_height_ft: float | None = None     # EXTRACT_IF_VISIBLE: else calc engine derives from eave + ridge
    roof_type: Literal["gable", "hip", "flat", "mono_slope", "gambrel", "mansard"] | None = None
    roof_pitch_deg: float | None = None          # EXTRACT_IF_VISIBLE: convert x/12 pitch to degrees; else calc engine derives
    number_of_stories: int | None = None
    story_heights_ft: List[float] | None = None  # EXTRACT: bottom-up, from sections/elevations
    floor_area_per_story_sf: List[float] | None = None  # EXTRACT_IF_VISIBLE: else calc engine uses L×W
    geometry_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'length_ft') where the value was unclear in the image."
    )


class PlanExtractedWindContext(BaseModel):
    """Wind info readable from plan general notes. Design wind speed itself
    comes from the hazard lookup — NOT extracted here."""
    exposure_category: Literal["B", "C", "D"] | None = None   # EXTRACT_IF_VISIBLE: from general notes
    exposure_justification: str = ""                          # EXTRACT_IF_VISIBLE: "suburban", "open terrain"
    enclosure_classification: Literal["enclosed", "partially_enclosed", "open"] = "enclosed"  # SKIP_EXTRACTION | default: enclosed
    topography_flat: bool = True                              # SKIP_EXTRACTION | default: True (Kzt=1.0)
    Kzt: float | None = None                                  # SKIP_EXTRACTION: HITL manual input when topography governs
    ground_elevation_ft: float | None = None                  # EXTRACT_IF_VISIBLE: site elevation from civil/general notes
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'topography_flat') where the value was unclear in the image."
    )


class PlanExtractedSeismicContext(BaseModel):
    """Seismic design parameters from general notes. SDS/SD1/SDC come from
    the USGS hazard lookup — NOT extracted here."""
    site_class: str | None = None                    # EXTRACT_IF_VISIBLE: from geotech report / general notes
    seismic_force_resisting_system: str = "Light-frame wood walls with wood structural panels"  # SKIP_EXTRACTION
    response_modification_R: float = 6.5             # EXTRACT_IF_VISIBLE | default: 6.5 (light-frame WSP, ASCE 7 Table 12.2-1)
    overstrength_omega0: float = 3.0                 # SKIP_EXTRACTION | default: 3.0
    deflection_amplification_Cd: float = 4.0         # SKIP_EXTRACTION | default: 4.0
    redundancy_rho: float = 1.0                      # SKIP_EXTRACTION | default: 1.0
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'overstrength_omega0') where the value was unclear in the image."
    )


class DesignLoads(BaseModel):
    """Design load table from general notes (psf). Used for gravity checks
    and seismic weight takeoff; nulls fall back to calc engine defaults."""
    roof_dead_load_psf: float | None = None          # EXTRACT_IF_VISIBLE: from design loads / general notes table
    roof_live_load_psf: float | None = None
    floor_dead_load_psf: float | None = None
    floor_live_load_psf: float | None = None
    wall_dead_load_psf: float | None = None
    design_loads_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'roof_snow_load_psf') where the value was unclear in the image."
    )


class ProjectContextData(BaseModel):
    """Single lightweight extraction call: geometry + site context + loads."""
    building_geometry: Optional[BuildingGeometry] = None
    wind_context: Optional[PlanExtractedWindContext] = None
    seismic_context: Optional[PlanExtractedSeismicContext] = None
    design_loads: Optional[DesignLoads] = None


# =====================================================================
# STRUCTURAL MEMBERS  (AI-extracted from plans)
# =====================================================================


# ----------------------------- Roof System ----------------------------

class RoofRafter(BaseModel):
    zone: str
    size: CanonicalMemberSize
    number_of_plies: int = 1
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    clear_span_ft: float | None = None
    clear_span_note: str = ""
    overhang_in: float | None = None
    overhang_note: str = ""
    roof_pitch: str | None = None                        # EXTRACT: "6/12", "4/12" etc.
    available_support_bearing_in: float | None = None
    available_support_bearing_note: str = ""
    support_material: SupportMaterial | None = None
    spacing_in: float | None = None
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'clear_span_ft') where the value was unclear in the image."
    )


class CeilingJoist(BaseModel):
    zone: str
    size: CanonicalMemberSize
    number_of_plies: int = 1
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    clear_span_ft: float | None = None
    clear_span_note: str = ""
    available_support_bearing_in: float | None = None
    available_support_bearing_note: str = ""
    support_material: SupportMaterial | None = None
    spacing_in: float | None = None
    ceiling_dead_load_psf: float | None = None
    attic_live_load_psf: float | None = None             # EXTRACT_IF_VISIBLE: 10 psf no storage, 20 psf limited storage
    attic_use: Literal["no_access", "limited_storage", "habitable"] = "limited_storage"
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'clear_span_ft') where the value was unclear in the image."
    )


class RidgeBeam(BaseModel):
    zone: str
    size: CanonicalMemberSize
    number_of_plies: int
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    clear_span_ft: float | None = None
    clear_span_note: str = ""
    available_support_bearing_in: float | None = None
    available_support_bearing_note: str = ""
    support_material: SupportMaterial | None = None
    tributary_width_ft: float | None = None
    tributary_width_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'clear_span_ft') where the value was unclear in the image."
    )


class HipValleyRafter(BaseModel):
    zone: str
    member_type: Literal["hip", "valley"]
    size: CanonicalMemberSize
    number_of_plies: int
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    clear_span_ft: float | None = None
    clear_span_note: str = ""
    roof_pitch: str | None = None
    available_support_bearing_in: float | None = None
    available_support_bearing_note: str = ""
    support_material: SupportMaterial | None = None
    tributary_width_ft: float | None = None
    tributary_width_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'clear_span_ft') where the value was unclear in the image."
    )


class RoofDropBeam(BaseModel):
    zone: str
    size: CanonicalMemberSize
    number_of_plies: int
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    clear_span_ft: float | None = None
    clear_span_note: str = ""
    roof_pitch: str | None = None
    available_support_bearing_in: float | None = None
    available_support_bearing_note: str = ""
    support_material: SupportMaterial | None = None
    tributary_width_ft: float | None = None
    tributary_width_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'clear_span_ft') where the value was unclear in the image."
    )


class RoofFlushBeam(BaseModel):
    zone: str
    size: CanonicalMemberSize
    number_of_plies: int
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    clear_span_ft: float | None = None
    clear_span_note: str = ""
    available_support_bearing_in: float | None = None
    available_support_bearing_note: str = ""
    support_material: SupportMaterial | None = None
    hanger_bucket_seat_depth_in: float | None = None
    hanger_seat_depth_note: str = ""
    hanger_carrier_material: str | None = None
    tributary_width_ft: float | None = None
    tributary_width_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'tributary_width_ft') where the value was unclear in the image."
    )


class RoofSystemData(BaseModel):
    roof_rafters: List[RoofRafter]
    ceiling_joists: List[CeilingJoist]
    ridge_beams: List[RidgeBeam]
    hip_valley_rafters: List[HipValleyRafter]
    roof_drop_beams: List[RoofDropBeam]
    roof_flush_beams: List[RoofFlushBeam]


# ----------------------------- Floor System ----------------------------

class FloorJoist(BaseModel):
    zone: str
    size: CanonicalMemberSize
    number_of_plies: int = 1
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    clear_span_ft: float | None = None
    clear_span_note: str | None = ""
    cantilever_ft: float | None = None
    cantilever_note: str | None = None
    available_support_bearing_in: float | None = None
    available_support_bearing_note: str | None = ""
    support_material: SupportMaterial | None = None
    spacing_in: float | None = None
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'clear_span_ft') where the value was unclear in the image."
    )


class FloorDropBeam(BaseModel):
    zone: str
    size: CanonicalMemberSize
    number_of_plies: int
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    clear_span_ft: float | None = None
    clear_span_note: str | None = ""
    available_support_bearing_in: float | None = None
    available_support_bearing_note: str | None = ""
    support_material: SupportMaterial | None = None
    tributary_width_ft: float | None = None
    tributary_width_note: str | None = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'clear_span_ft') where the value was unclear in the image."
    )


class FloorFlushBeam(BaseModel):
    zone: str
    size: CanonicalMemberSize
    number_of_plies: int
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    clear_span_ft: float | None = None
    clear_span_note: str | None = ""
    available_support_bearing_in: float | None = None
    available_support_bearing_note: str | None = ""
    support_material: SupportMaterial | None = None
    tributary_width_ft: float | None = None
    tributary_width_note: str | None = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'clear_span_ft') where the value was unclear in the image."
    )


class FloorSystemData(BaseModel):
    floor_joists: List[FloorJoist]
    floor_drop_beams: List[FloorDropBeam]
    floor_flush_beams: List[FloorFlushBeam]


# ----------------------------- Footing System -------------------------
class ContinuousStripFooting(BaseModel):
    footing_mark: str
    existing_condition: bool = False
    new_load_applied: str = ""
    location_description: str
    supported_element: str | None = None             # wall mark/zone this footing supports — links to wall reactions
    width_in: float | None = None
    width_note: str = ""
    depth_in: float | None = None
    depth_note: str = ""
    longitudinal_rebar_size: int | None = None       # EXTRACT: bar number of continuous bars (#4 = 4), e.g. "2-#4 CONT."
    longitudinal_rebar_count: int | None = None      # EXTRACT: count of continuous longitudinal bars
    transverse_rebar_size: int | None = None
    transverse_rebar_spacing_in: float | None = None
    sill_plate_size: CanonicalMemberSize | None = None
    axial_D_kips: float | None = None                # EXTRACT_IF_VISIBLE: only if loads explicitly stated on plans; also HITL manual input
    axial_L_kips: float | None = None
    axial_S_kips: float | None = None
    footing_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'width_in') where the value was unclear in the image."
    )


class PadFooting(BaseModel):
    footing_mark: str
    existing_condition: bool = False
    new_load_applied: str = ""
    location_description: str
    supported_element: str
    width_in: float | None = None
    length_in: float | None = None
    depth_in: float | None = None
    dimension_note: str = ""
    embedment_note: str = ""
    rebar_size: int | None = None
    rebar_spacing_in: float | None = None
    post_size: CanonicalMemberSize | None = None
    axial_DL_lbs: float | None = None                # EXTRACT_IF_VISIBLE: only if loads explicitly stated on plans; also HITL manual input
    axial_LL_lbs: float | None = None
    axial_SL_lbs: float | None = None
    footing_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'width_in') where the value was unclear in the image."
    )


class FootingProjectInfo(BaseModel):
    soil_bearing_pressure_psf: float | None = None
    concrete_fc_footings_psi: float | None = None
    concrete_fc_slab_psi: float | None = None


class FootingSystemData(BaseModel):
    project_info: FootingProjectInfo | None = None
    continuous_strip_footings: List[ContinuousStripFooting]
    pad_footings: List[PadFooting]


# ----------------------------- Post System ----------------------------

class StandalonePost(BaseModel):
    post_mark: str
    existing_condition: bool = False
    # new_load_applied: str = ""
    location_description: str
    post_type: Literal["solid", "built_up"] | None = None
    functional_type: Literal[
        "bearing", "holdown", "corner",
    ] | None = None
    post_size: CanonicalMemberSize
    number_of_plies: int | None = None
    species: Literal["DF-L", "SP", "SPF", "HF", "LVL", "PSL", "LSL", "glulam"] | None = None
    grade: Literal["SS", "No.1", "No.2", "No.3", "Stud", "1.55E", "2.0E", "2.2E", "24F-V4"] | None = None
    ewp_spec: EWPSpec | None = None
    species_grade_note: str = ""
    height_ft: float | None = None
    height_note: str = ""
    unbraced_length_ft: float | None = None
    unbraced_length_note: str = ""
    bracing_condition: str = ""
    tributary_area_sf: float | None = None
    tributary_area_note: str = ""
    point_load_lbs: float | None = None
    point_load_note: str = ""
    base_connector_model: str = ""
    base_bearing_surface: Literal[
        "concrete_footing", "concrete_slab",
        "wood_beam", "steel_beam", "grade"
    ] | None = None
    base_anchor_type: Literal["cast_in", "epoxy_set", "bolt_through"] | None = None
    post_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'height_ft') where the value was unclear in the image."
    )


class PostData(BaseModel):
    standalone_posts: List[StandalonePost]


# ----------------------------- Shear Wall System ----------------------

class WallOpening(BaseModel):
    """An opening in a shear wall. Null means NOT FOUND (flagged for review);
    never default to 0 — zero is a real value (a door has sill_height_ft = 0)."""
    width_ft: float | None = None
    height_ft: float | None = None
    sill_height_ft: float | None = None        # sheathed depth below opening; 0 for doors
    distance_from_wall_left_ft: float | None = None  # wall's left end -> opening's left edge, as drawn (same orientation as pier_lengths_ft)


class ShearWall(BaseModel):
    sw_mark: str
    swl_id: str
    story_level: str
    system_type: str
    shear_wall_method: Literal["segmented", "perforated", "ftao"] | None = None
    pier_lengths_ft: List[float] | None = None
    openings: List[WallOpening] | None = None
    framing_species: str | None = None
    pier_length_ft: float | None = None
    wall_height_ft: float | None = None
    sheathing_type: str
    sheathing_thickness_in: float | None = None
    sheathing_faces: Literal["one_side", "two_sides"] | None = None
    edge_nail_spacing_in: float | None = None
    boundary_nail_spacing_in: float | None = None
    nail_size: Literal["10d", "8d"] | None = None
    stud_size: CanonicalMemberSize | None = None
    stud_spacing_in: float | None = None
    holdown_model: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'wall_height_ft') where the value was unclear in the image."
    )

class DiaphragmNailingZone(BaseModel):
    zone_label: str
    boundary_nail_spacing_in: float | None = None
    field_nail_spacing_in: float | None = None
    area_description: str = ""


class Diaphragm(BaseModel):
    """A floor/roof diaphragm per level, from the diaphragm schedule and
    framing plan general notes. Demands come from the lateral analysis;
    capacity comes from the schedule's nominal unit shear when stated."""
    level: str
    direction: Literal["X", "Y", "both"] | None = None
    span_ft: float | None = None                 # between lateral lines, EXTRACT_IF_VISIBLE
    depth_ft: float | None = None                # parallel to lateral lines, EXTRACT_IF_VISIBLE
    sheathing_type: str = ""                     # "15/32 WSP", "3/4 T&G", ...
    sheathing_thickness_in: float | None = None
    nail_size: str = ""                          # "10d", "8d"
    blocked: bool | None = None
    nailing_zones: List[DiaphragmNailingZone] | None = None
    nominal_unit_shear_plf: float | None = None  # from schedule, EXTRACT_IF_VISIBLE
    chord_member: str | None = None              # e.g. "dbl top plate", "rim + strap"
    collector_lines: List[str] | None = None
    diaphragm_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'span_ft') where the value was unclear in the image."
    )


class ShearWallData(BaseModel):
    shear_walls: List[ShearWall]
    diaphragms: List[Diaphragm] = []


# ----------------------------- Wall System --------------------

class MasonryWall(BaseModel):
    """A reinforced masonry (CMU/clay) wall panel for TMS 402 design.

    Out-of-plane lateral demand (wind psf, SDS) comes from the hazard
    lookup / wind context via the calc engine — not extracted per wall.
    """
    wall_mark: str
    story_level: str = ""
    nominal_thickness_in: float | None = None        # 7.625 for 8" CMU
    wall_height_ft: float | None = None
    panel_length_ft: float | None = None
    fm_psi: float | None = None
    unit_compressive_strength_psi: float | None = None
    grouting: Literal["fully_grouted", "partially_grouted", "ungrouted"] | None = None
    mortar_type: Literal["M", "S", "N", "O"] | None = None
    vertical_bar_size: str = ""                      # "#5", "#6", ...
    vertical_bar_spacing_in: float | None = None
    horizontal_bar_size: str = ""                    # "#4", "#5", ... for in-plane shear reinf.
    horizontal_bar_spacing_in: float | None = None
    rebar_grade_ksi: float | None = None             # 60 typ.
    axial_dead_plf: float | None = None              # roof/floor DL at top
    axial_live_plf: float | None = None
    roof_snow_load_psf: float | None = None
    seismic_participation: Literal["participating", "nonparticipating"] | None = None
    swl_id: str = ""
    masonry_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'wall_height_ft') where the value was unclear in the image."
    )

class StudWall(BaseModel):
    zone: str
    # wall_type: Literal["exterior_bearing", "interior_bearing", "non_bearing_partition", "shear_wall"]
    stud_size: CanonicalMemberSize
    number_of_plies: int = 1
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    stud_height_ft: float | None = None
    stud_height_note: str = ""
    spacing_in: float = 16.0                      # EXTRACT_IF_VISIBLE | default: 16" o.c.
    wall_length_ft: float | None = None
    wall_length_note: str = ""
    support_material: SupportMaterial | None = None
    tributary_width_ft: float | None = None
    wall_dead_load_psf: float | None = None
    stud_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'stud_height_ft') where the value was unclear in the image."
    )


class Header(BaseModel):
    zone: str
    opening_mark: str | None = None
    header_size: CanonicalMemberSize | None = None
    number_of_plies: int | None = None
    lumber_spec: LumberSpec | None = None
    ewp_spec: EWPSpec | None = None
    header_clear_span_ft: float | None = None
    header_clear_span_note: str = ""
    available_bearing_in: float | None = None
    available_bearing_note: str = ""
    support_material: SupportMaterial | None = None
    tributary_width_ft: float | None = None
    tributary_width_note: str = ""
    point_load_lbs: float | None = None
    roof_load_on_header_psf: float | None = None
    header_note: str = ""
    uncertain_areas: List[str] | None = Field(
        default=None,
        description="List ONLY the exact python variable names from this schema (e.g., 'header_clear_span_ft') where the value was unclear in the image."
    )


class WallSystemData(BaseModel):
    stud_walls: List[StudWall]
    headers: List[Header]
    masonry_walls: List[MasonryWall]