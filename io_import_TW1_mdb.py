bl_info = {
    "name": "The Witcher 1 MDB Importer",
    "author": "Angry Catster",
    "version": (1, 1, 0),
    "blender": (5, 0, 1),
    "location": "File > Import > Witcher MDB (.mdb) / Witcher Animation (.mba)",
    "description": "Import The Witcher 1 .mdb model files and .mba animation packs",
    "category": "Import-Export",
}

import bpy
import struct
import os
import sys
import traceback
import math
import subprocess
import re
import bisect
from mathutils import Vector, Matrix, Quaternion
from collections import defaultdict
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, BoolProperty, FloatProperty, EnumProperty, IntProperty
from bpy.types import Operator

# Node Types
NODE_TYPE_NODE = 0x00000001
NODE_TYPE_LIGHT = 0x00000003
NODE_TYPE_EMITTER = 0x00000005
NODE_TYPE_TRIMESH = 0x00000021
NODE_TYPE_SKIN = 0x00000061
NODE_TYPE_TEXTURE_PAINT = 0x00008001
NODE_TYPE_SPEEDTREE = 0x00010001

# Controller types
CONTROLLER_POSITION = 84
CONTROLLER_ORIENTATION = 96
CONTROLLER_SCALE = 184
CONTROLLER_SELF_ILLUM_COLOR = 276
CONTROLLER_ALPHA = 292

# Column count is the low nibble of this byte; the high nibble says how many
# value sets each row carries, of which only the first is the value.
CONTROLLER_COLUMN_MASK = 0x0F
CONTROLLER_ROW_SETS = {0x00: 1, 0x10: 3, 0x20: 4, 0x40: 2}

# The rate the packs were authored at.
ANIMATION_FPS = 30.0
ANIMATION_FRAME_SNAP = 0.02
ANIMATION_LOOP_TOLERANCE = 1e-3

# File versions
FILE_VERSION_133 = 133
FILE_VERSION_136 = 136

# Custom properties written on armature bones by the MDB importer. They hold the
# rest transforms of the MDB node a bone came from, which the MBA animation
# importer needs to map node-space animation onto Blender's own bone rest frames.
REST_LOCAL_PROP = "tw1_rest_local"
REST_GLOBAL_PROP = "tw1_rest_global"


class CompositeModel:
    """A 'binarycompositemodel' - a plain-text stub naming a real model."""

    def __init__(self):
        self.name = ""
        self.base_model = ""
        self.animation_sets = []


def read_composite_model(filepath):
    """Return a CompositeModel for a composite stub, or None for a real model."""
    try:
        with open(filepath, 'rb') as handle:
            head = handle.read(4096)
    except OSError:
        return None

    if not head[:1] or head[:1] == b'\0':
        return None
    text = head.split(b'\0')[0].decode('latin-1', 'replace')
    if not text.lstrip().startswith("binarycompositemodel"):
        return None

    composite = CompositeModel()
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "binarycompositemodel":
            composite.name = parts[1] if len(parts) > 1 else ""
            composite.base_model = parts[2] if len(parts) > 2 else ""
        elif parts[0] == "animationset" and len(parts) > 1:
            composite.animation_sets.append(parts[1])
        elif parts[0] == "donecompositemodel":
            break
    return composite if composite.base_model else None


def find_sibling_model(filepath, model_name):
    """Locate the .mdb a composite model refers to, next to the composite."""
    if not model_name or model_name.upper() == "NULL":
        return None
    folder = os.path.dirname(filepath)
    candidate = os.path.join(folder, model_name + ".mdb")
    if os.path.exists(candidate):
        return candidate
    # Fall back to a case-insensitive match; the game's own paths are sloppy.
    target = (model_name + ".mdb").lower()
    try:
        for entry in os.listdir(folder):
            if entry.lower() == target:
                return os.path.join(folder, entry)
    except OSError:
        pass
    return None


# The render pass the game sorts a node into. It settles blending only -
# alpha-tested geometry still rides in the opaque pass.
RENDER_PASS_BLENDED = frozenset(("TRSP", "SKY_", "SCRN", "FLAR", "CRNS", "TRUG"))

# The sky's sun and moon. Every skybox in the game names them this way -
# _sky_sun and _sky_moon, 21 nodes between them - and they are the only sky
# elements that sit inside the shells rather than enclosing the viewer.
CELESTIAL_NODE_PREFIX = "_sky_"

# The sky's weather layer, which the game only fades in when the weather turns.
WEATHER_CLOUD_SHADERS = frozenset(("clouds_weather",))
WEATHER_OVERLAY_PROP = "tw1_weather_overlay"

# Passes drawn over the finished scene: overlays, never depth writers. SKY_ is
# deliberately not one of them.
RENDER_PASS_EFFECT_LAYERS = frozenset(("SCRN", "FLAR", "CRNS", "TRUG"))


def render_pass_tag(mesh_data):
    """The node's four-character render pass tag, or None if it has none."""
    value = mesh_data.get('render_pass')
    if value is None:
        return None
    try:
        tag = struct.pack("<I", value & 0xFFFFFFFF).decode('ascii')
    except (struct.error, UnicodeDecodeError):
        return None
    return tag if tag.isprintable() else None


# Shaders whose name says outright that they draw something see-through.
# A floor, not a verdict: a texture that measures as coverage gets it anyway.
SHADERS_DECLARING_TRANSPARENCY = frozenset((
    "transparency_2p", "transparency_2ps", "trans_cds_2p",
    "skin_all_trans", "norm_all_trans", "alphamask", "dblsided_atest",
    "leaves", "leaves_lm", "leaves_lm_bill", "leaves_singles", "plant", "hair",
    "envmap_alpha", "additive_alpha", "additive_alphaz",
    "dadd_alpha_mul", "dadd_al_mul_alp",
))

# The window shaders. Their alpha is a light mask, not coverage, and the game
# lights those panes at night.
WINDOW_GLOW_SHADERS = frozenset(("envmapping_lm_b", "envmap_lm_b_sic"))

# Lit when the level's own lighting is, which is what the time of day selects.
WINDOW_GLOW_TIMES = frozenset(("NIGHT", "MORNING"))

# Lamplight - the one number here the files do not supply.
WINDOW_GLOW_COLOR = (1.0, 0.73, 0.36)
WINDOW_GLOW_STRENGTH = 2.0


def window_glow_applies(mesh_data, time_of_day):
    """True when this node is a window that should be lit from inside."""
    return ((mesh_data.get('shader_type') or '') in WINDOW_GLOW_SHADERS
            and time_of_day in WINDOW_GLOW_TIMES)


# Shaders painted on a closed surface, which is not four-fifths holes, so their
# alpha needs a higher bar before it reads as coverage.
SOLID_SURFACE_SHADERS = frozenset((
    "specular",
    "envmapping", "envmapping_lm", "envmapping_lm_b", "envmap_s",
    "envmap_lm_b_sic", "envmap_lmtp", "envmap_lmtp_b",
    "envadd", "envadd_lm", "envadd_lm_b", "envadd_lmtp", "envadd_lmtp_b",
    "normalmap_env", "norm_env_rim_ao", "norm_env_rim_l", "skin_nrimaoenv",
    "selfilum", "selfilum_b", "normalmap_selfil", "normalmap_glow", "skin_n_glow",
    "skin_n", "skin_n_rim_ao", "skin_n_rim_ao_mh", "skin_n_rim_ao_md",
    "reflection", "reflection_b", "simple_refl",
    "texture_blend", "texture_blend_2p",
    "noalphatest",
))


# When neither the pass nor the shader settles it, the texture does: a cut-out's
# alpha is a stencil, a mask is a ramp.
COVERAGE_MIN_EXTREME_FRACTION = 0.7
# ...and enough of the surface has to actually draw, measured above 0.4 rather
# than at full opacity.
COVERAGE_MIN_PRESENT_FRACTION = 0.02
COVERAGE_MIN_PRESENT_ON_SURFACE = 0.5
# A cut-out needs holes as well as substance: window masks have none.
COVERAGE_MIN_EMPTY_FRACTION = 0.01

_alpha_coverage_cache = {}


def texture_alpha_is_coverage(image, solid_surface=False):
    """True when a texture's alpha reads as a cut-out rather than a mask."""
    if image is None or image.channels < 4:
        return False

    key = (image.filepath_raw or image.filepath or image.name, solid_surface)
    cached = _alpha_coverage_cache.get(key)
    if cached is not None:
        return cached

    result = True
    try:
        import numpy
        width, height = image.size
        buf = numpy.empty(width * height * image.channels, dtype=numpy.float32)
        image.pixels.foreach_get(buf)
        alpha = buf.reshape(-1, image.channels)[:, 3]
        empty = float((alpha < 0.04).mean())
        solid = float((alpha > 0.9).mean())
        present = float((alpha > 0.4).mean())
        floor = (COVERAGE_MIN_PRESENT_ON_SURFACE if solid_surface
                 else COVERAGE_MIN_PRESENT_FRACTION)
        result = (empty + solid >= COVERAGE_MIN_EXTREME_FRACTION
                  and present >= floor
                  and empty >= COVERAGE_MIN_EMPTY_FRACTION)
    except Exception as e:
        logger.log(f"  Could not read alpha of {key}: {e}")
        return True

    _alpha_coverage_cache[key] = result
    return result


# Materials built by the import that is running. Cleared at the start of every
# import: they are shared within one import and never across two.
_import_materials = {}

_alpha_trivial_cache = {}


def texture_alpha_is_trivial(image):
    """True when a texture's alpha is solid everywhere, so nothing can blend."""
    if image is None:
        return True
    if image.channels < 4:
        return True

    key = image.filepath_raw or image.filepath or image.name
    cached = _alpha_trivial_cache.get(key)
    if cached is not None:
        return cached

    result = False
    try:
        import numpy
        width, height = image.size
        buf = numpy.empty(width * height * image.channels, dtype=numpy.float32)
        image.pixels.foreach_get(buf)
        alpha = buf.reshape(-1, image.channels)[:, 3]
        result = bool((alpha > 0.99).all())
    except Exception as e:
        logger.log(f"  Could not read alpha of {key}: {e}")
        return False

    _alpha_trivial_cache[key] = result
    return result


ALPHA_OPAQUE = 'OPAQUE'
ALPHA_CLIP = 'CLIP'
ALPHA_BLEND = 'BLEND'
# Clip, but only once the diffuse texture has been seen and its alpha turns out
# to be a cut-out rather than a mask.
ALPHA_CLIP_IF_COVERAGE = 'CLIP_IF_COVERAGE'


def resolve_alpha_mode(mesh_data):
    """Decide how one mesh node should treat its diffuse alpha."""
    if render_pass_tag(mesh_data) in RENDER_PASS_BLENDED:
        return ALPHA_BLEND

    alpha = mesh_data.get('alpha')
    if alpha is not None and alpha < 1.0:
        return ALPHA_BLEND
    if mesh_data.get('transparency_hint') or mesh_data.get('is_transparent'):
        return ALPHA_BLEND

    if (mesh_data.get('shader_type') or '') in SHADERS_DECLARING_TRANSPARENCY:
        return ALPHA_CLIP
    return ALPHA_CLIP_IF_COVERAGE


def resolve_deferred_alpha_mode(mode, image, mesh_data=None):
    """Settle CLIP_IF_COVERAGE now that the diffuse texture is loaded."""
    if mode != ALPHA_CLIP_IF_COVERAGE:
        return mode
    solid = (mesh_data or {}).get('shader_type') in SOLID_SURFACE_SHADERS
    return ALPHA_CLIP if texture_alpha_is_coverage(image, solid) else ALPHA_OPAQUE


def apply_alpha_mode(mat, bsdf, mode, alpha_value=1.0):
    """Set up a material for the alpha mode, before anything is linked in."""
    if mode == ALPHA_CLIP_IF_COVERAGE:
        mode = ALPHA_CLIP
    if mode == ALPHA_BLEND:
        mat.blend_method = 'BLEND'
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'BLENDED'
        bsdf.inputs['Alpha'].default_value = alpha_value
    elif mode == ALPHA_CLIP:
        mat.blend_method = 'CLIP'
        if hasattr(mat, 'alpha_threshold'):
            mat.alpha_threshold = 0.5
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'DITHERED'
    else:
        mat.blend_method = 'OPAQUE'
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'DITHERED'


def link_texture_alpha(links, nodes, tex_node, bsdf, mode, alpha_value=1.0,
                       mesh_data=None):
    """Feed a texture's alpha into the shader for the modes that want it."""
    image = tex_node.image
    if image is None or image.channels < 4:
        return False
    if resolve_deferred_alpha_mode(mode, image, mesh_data) == ALPHA_OPAQUE:
        return False

    source = tex_node.outputs['Alpha']
    if alpha_value < 1.0:
        # The node's own alpha controller scales the texture's alpha rather than being
        # replaced by it.
        scale = nodes.new('ShaderNodeMath')
        scale.location = (tex_node.location.x + 220, tex_node.location.y - 160)
        scale.operation = 'MULTIPLY'
        scale.label = "Node Alpha"
        scale.inputs[1].default_value = alpha_value
        links.new(source, scale.inputs[0])
        source = scale.outputs['Value']

    links.new(source, bsdf.inputs['Alpha'])
    return True


def fill_unpainted_weights(mesh, layer_weights):
    """Spread paint into vertices that no surviving layer covers.

    layer_weights is one list of per-vertex weights per surviving layer, edited
    in place. Returns the number of vertices that had to be filled.
    """
    if not layer_weights:
        return 0

    vertex_count = len(layer_weights[0])
    painted = [sum(w[v] for w in layer_weights) > 1e-4 for v in range(vertex_count)]
    missing = [v for v in range(vertex_count) if not painted[v]]
    if not missing or len(missing) == vertex_count:
        return 0

    neighbours = [[] for _ in range(vertex_count)]
    for edge in mesh.edges:
        a, b = edge.vertices
        if a < vertex_count and b < vertex_count:
            neighbours[a].append(b)
            neighbours[b].append(a)

    filled = 0
    frontier = list(missing)
    while frontier:
        resolved = []
        for v in frontier:
            sources = [n for n in neighbours[v] if painted[n]]
            if not sources:
                continue
            for w in layer_weights:
                w[v] = sum(w[n] for n in sources) / len(sources)
            resolved.append(v)
        if not resolved:
            break
        for v in resolved:
            painted[v] = True
            filled += 1
        frontier = [v for v in frontier if not painted[v]]

    return filled


def texture_paint_layers(mesh_data, importer):
    """Layers that own a texture, in the order their weight channels are packed."""
    vertex_count = len(mesh_data.get('vertices') or ())
    return [(i, layer) for i, layer in enumerate(mesh_data.get('layers') or ())
            if layer.get('texture') and layer.get('weights')
            and len(layer['weights']) == vertex_count
            and importer.find_texture_file(layer['texture'])]


def matrix_to_list(matrix):
    return [c for row in matrix for c in row]


def list_to_matrix(values):
    return Matrix([tuple(values[i * 4:i * 4 + 4]) for i in range(4)])

# Arbitrary scale multiplier for tree meshes
TREE_SCALE_MULTIPLIER = 32.0

# Texture keys that name the surface's own colour map, and the slot each belongs
# in. A list of what to take, not what to skip.
DIFFUSE_TEXTURE_KEYS = {
    "texture0": 0, "texture1": 1, "texture2": 2, "texture3": 3,
    "tex": 0, "texture_layer0": 0,
    "diff_texture": 0, "diffuse_texture": 0, "diffuse_map": 0,
    "main_texture": 0, "mainTexture": 0, "leaves_texture": 0,
}

# Meshes whose texture could not be resolved are left faint rather than drawn as
# solid white blocks in front of everything else.
UNTEXTURED_MATERIAL_ALPHA = 0.2

# Shaders whose name says the surface is drawn from both sides. No mesh in the
# game carries its own back faces.
DOUBLE_SIDED_SHADERS = frozenset(("dblsided_atest", "double_sided"))

# Shaders that add what they draw to the scene instead of covering it. The
# "dadd" family modulates the first stage with a second.
ADDITIVE_SHADERS = frozenset((
    "additive", "additive_alpha", "additive_alphaz",
    "dadd_alpha_mul", "dadd_al_mul_alp", "double_add", "decal_additive",
    # A lens flare: both corona textures are black but for a bright core, with
    # alpha 1 from edge to edge. Blended, that is a black square over the sun.
    "corona",
))


def resolve_lightmap_name(lightmap_texture, light_map_name, valid_textures):
    """Which of a node's textures is its lightmap, by name."""
    if lightmap_texture and lightmap_texture in valid_textures:
        return lightmap_texture
    if light_map_name and light_map_name in valid_textures:
        return light_map_name
    return None


def uv_slot_for(t_verts_defs, stage):
    """The UV set a texture stage samples, given what the mesh actually carries.

    Returns -1 when the mesh carries no UVs at all.
    """
    occupied = [i for i, d in enumerate(t_verts_defs[:4]) if d.nb_used_entries > 0]
    if not occupied:
        return -1
    return stage if stage in occupied else occupied[0]


def material_stage_textures(material_params, fallback):
    """The colour maps a material names, in texture-stage order."""
    textures = (material_params or {}).get('textures') or {}
    staged = []
    for key, name in textures.items():
        slot = DIFFUSE_TEXTURE_KEYS.get(key)
        if slot is not None and name:
            staged.append((slot, name))
    if not staged:
        return list(fallback or [])
    staged.sort()
    ordered = []
    for _, name in staged:
        if name not in ordered:
            ordered.append(name)
    return ordered


def is_double_sided(mesh_data):
    """True when the node's shader draws the surface from both sides."""
    return (mesh_data.get('shader_type') or '') in DOUBLE_SIDED_SHADERS


def apply_backface_culling(mat, mesh_data):
    """Cull back faces unless the shader says the surface is two-sided."""
    mat.use_backface_culling = not is_double_sided(mesh_data)


def build_additive_output(nodes, links, colour, strength, location=(100, 0)):
    """Wire a colour and a strength into an additive surface."""
    emission = nodes.new('ShaderNodeEmission')
    emission.location = (location[0] - 250, location[1])
    links.new(colour, emission.inputs['Color'])
    if strength is not None:
        links.new(strength, emission.inputs['Strength'])

    transparent = nodes.new('ShaderNodeBsdfTransparent')
    transparent.location = (location[0] - 250, location[1] - 200)

    add = nodes.new('ShaderNodeAddShader')
    add.location = location
    links.new(emission.outputs['Emission'], add.inputs[0])
    links.new(transparent.outputs['BSDF'], add.inputs[1])

    output = nodes.new('ShaderNodeOutputMaterial')
    output.location = (location[0] + 200, location[1])
    links.new(add.outputs['Shader'], output.inputs['Surface'])
    return emission


# Shaders that draw light onto a surface rather than the surface itself.
CAUSTIC_SHADERS = frozenset(("caustic",))

# Shaders whose name says the surface is a mirror. Their nodes also set the
# needsReflection flag and carry a reflection plane, which is how the game knows
# to render the scene again into them.
MIRROR_SHADERS = frozenset(("reflection", "reflection_b", "simple_refl"))

# The file says "mirror" and nothing more, so a polished dielectric is as far as
# the data goes.
MIRROR_ROUGHNESS = 0.12
MIRROR_SPECULAR = 0.5
# How far from its plane a reflection probe reaches. Enough to cover a floor
# that is not perfectly flat, short enough not to claim what stands on it.
REFLECTION_PROBE_INFLUENCE = 1.0


def is_mirror_surface(mesh_data):
    """True when the file marks a node as a mirror the game re-renders into."""
    return (bool(mesh_data.get('needs_reflection'))
            and (mesh_data.get('shader_type') or '') in MIRROR_SHADERS)


def create_reflection_probe(obj, mesh_data):
    """Give a mirror surface the planar reflection probe EEVEE needs."""
    # The mesh's own surface is the plane; the stored normal is the fallback for
    # degenerate meshes.
    world_normal = Vector()
    for poly in obj.data.polygons:
        world_normal += poly.normal
    if world_normal.length < 1e-6:
        world_normal = Vector(mesh_data.get('reflection_plane_normal') or (0.0, 0.0, 1.0))
    world_normal = obj.matrix_world.to_3x3() @ world_normal
    if world_normal.length < 1e-6:
        return None
    world_normal.normalize()

    corners = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
    centre = sum(corners, Vector()) / len(corners)
    extent = max((c - centre).length for c in corners)

    probe_data = bpy.data.lightprobes.new(name=f"Reflection_{obj.name}", type='PLANE')
    probe_data.influence_distance = REFLECTION_PROBE_INFLUENCE
    probe = bpy.data.objects.new(f"Reflection_{obj.name}", probe_data)
    probe.location = centre
    probe.rotation_euler = world_normal.to_track_quat('Z', 'Y').to_euler()
    probe.scale = (max(extent, 0.1), max(extent, 0.1), 1.0)
    probe["tw1_node_type"] = "reflection_plane"
    return probe


def scroll_speeds(material_params, stage):
    """The (u, v) speed of one of a material's texture matrices."""
    floats = (material_params or {}).get('floats') or {}
    u = floats.get(f"matrix_scroll_{stage}_speed_u", 0.0)
    v = floats.get(f"matrix_scroll_{stage}_speed_v", 0.0)
    return float(u), -float(v)


def add_scroll_driver(mapping_node, speed_u, speed_v):
    """Make a Mapping node's offset advance with the timeline."""
    if not speed_u and not speed_v:
        return
    for index, speed in ((0, speed_u), (1, speed_v)):
        if not speed:
            continue
        fcurve = mapping_node.inputs['Location'].driver_add('default_value', index)
        driver = fcurve.driver
        driver.type = 'SCRIPTED'
        for name, path in (("fps", "render.fps"), ("fps_base", "render.fps_base")):
            var = driver.variables.new()
            var.name = name
            var.type = 'SINGLE_PROP'
            target = var.targets[0]
            target.id_type = 'SCENE'
            target.id = bpy.context.scene
            target.data_path = path
        driver.expression = (f"(frame - 1) * fps_base / fps * {speed:.6f}"
                             " if fps else 0.0")

# Texture search folders
TEXTURE_FOLDERS = [
    "textures00/",
    "textures01/",
    "meshes/",
    "meshes00/",
    "items00/",
    "trees00/",
]

TEXTURE_EXTENSIONS = [".dds", ".tga"]

TIME_OF_DAY_SUFFIXES = {
    'DAY': "!d",
    'MORNING': "!r",
    'NOON': "!p",
    'EVENING': "!w",
    'NIGHT': "!n",
    'NONE': ""
}

TIME_RANGES = {
    'MORNING': (7, 9),    # 7:00 - 9:00 inclusive
    'DAY': (10, 20),      # 10:00 - 20:00 inclusive
    'EVENING': (10, 20),  # same as day
    'NIGHT': (21, 6),     # 21:00 - 6:00 inclusive (wraps around)
    'NOON': (10, 20),     # same as day
}

DEFAULT_HOURS = {
    'MORNING': 8,
    'DAY': 15,
    'EVENING': 18,
    'NIGHT': 0,
    'NOON': 12,
}

# List of node name patterns to skip (case-insensitive partial matching)
SKIP_NODE_PATTERNS = [
    "shadowbon", "shadowsword", # used by the game for the stencil shadows
    "door_coll_dummy",           # door collision mesh
    "Pyramid01", "clickable",    # collision for the bushes
    "flarka", "sun_dummy", "cien", "blendbox", "woda_walkmesh", "Wm_woda", "fx_hitcheck03", "fx_hitcheck02", "fx_hitcheck01"
]

TEXTURE_NAME_MAPPINGS = {
    "l34_skull": "karczma_wnetrze", # Act 1 tavern
    "l35_skull": "mur_szach_15",    # Act 2 tavern
    "l02_skull": "mur_freski_03",   # Kaer Morhen - interior. Wrong texture, but idk where the right one is
}

class MDBLogger:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.indent = 0
        self.silent_node_types = [
            NODE_TYPE_NODE
        ]
    
    def log(self, message, force=False):
        if self.enabled or force:
            indent_str = "  " * self.indent
            print(f"{indent_str}{message}")
    
    def log_node(self, node_name, node_type, message, force=False):
        if force or node_type not in self.silent_node_types:
            self.log(message)
    
    def error(self, message):
        print(f"ERROR: {message}", file=sys.stderr)
    
    def warning(self, message):
        print(f"WARNING: {message}")
    
    def push(self):
        self.indent += 1
    
    def pop(self):
        self.indent -= 1

logger = MDBLogger()

class BinaryReader:
    def __init__(self, filepath):
        self.filepath = filepath
        self.file = None
        self.size = 0
        
        try:
            self.file = open(filepath, "rb")
            self.file.seek(0, 2)
            self.size = self.file.tell()
            self.file.seek(0, 0)
            logger.log(f"Opened file: {filepath} ({self.size} bytes)")
        except Exception as e:
            logger.error(f"Failed to open file: {e}")
            raise
    
    def close(self):
        if self.file:
            self.file.close()
    
    def seek(self, offset, whence=0):
        try:
            self.file.seek(offset, whence)
        except Exception as e:
            logger.error(f"Seek error at offset {offset}: {e}")
            raise
    
    def tell(self):
        return self.file.tell()
    
    def read_u8(self):
        data = self.file.read(1)
        if len(data) < 1:
            raise EOFError(f"Unexpected EOF at {self.tell()}")
        return struct.unpack("B", data)[0]
    
    def read_s8(self):
        data = self.file.read(1)
        if len(data) < 1:
            raise EOFError(f"Unexpected EOF at {self.tell()}")
        return struct.unpack("b", data)[0]
    
    def read_u16(self):
        data = self.file.read(2)
        if len(data) < 2:
            raise EOFError(f"Unexpected EOF at {self.tell()}")
        return struct.unpack("<H", data)[0]
    
    def read_s16(self):
        data = self.file.read(2)
        if len(data) < 2:
            raise EOFError(f"Unexpected EOF at {self.tell()}")
        return struct.unpack("<h", data)[0]
    
    def read_u32(self):
        data = self.file.read(4)
        if len(data) < 4:
            raise EOFError(f"Unexpected EOF at {self.tell()}")
        return struct.unpack("<I", data)[0]
    
    def read_s32(self):
        data = self.file.read(4)
        if len(data) < 4:
            raise EOFError(f"Unexpected EOF at {self.tell()}")
        return struct.unpack("<i", data)[0]
    
    def read_f32(self):
        data = self.file.read(4)
        if len(data) < 4:
            raise EOFError(f"Unexpected EOF at {self.tell()}")
        return struct.unpack("<f", data)[0]
    
    def read_f64(self):
        data = self.file.read(8)
        if len(data) < 8:
            raise EOFError(f"Unexpected EOF at {self.tell()}")
        return struct.unpack("<d", data)[0]
    
    def read_string(self, length):
        try:
            data = self.file.read(length)
            if len(data) < length:
                raise EOFError(f"Unexpected EOF reading string of length {length}")
            null_pos = data.find(b'\x00')
            if null_pos != -1:
                data = data[:null_pos]
            return data.decode('utf-8', errors='ignore').strip()
        except Exception as e:
            logger.error(f"Error reading string at {self.tell()}: {e}")
            return ""
    
    def read_string_until_null(self):
        chars = []
        try:
            while True:
                c = self.file.read(1)
                if not c or c == b'\x00':
                    break
                chars.append(c.decode('utf-8', errors='ignore'))
        except Exception as e:
            logger.error(f"Error reading null-terminated string: {e}")
        return ''.join(chars).strip()
    
    def read_c_string(self):
        return self.read_string_until_null()

class ArrayDef:
    def __init__(self):
        self.first_elem_offset = 0
        self.nb_used_entries = 0
        self.nb_allocated_entries = 0
    
    @classmethod
    def read(cls, reader):
        def_ = cls()
        def_.first_elem_offset = reader.read_u32()
        def_.nb_used_entries = reader.read_u32()
        def_.nb_allocated_entries = reader.read_u32()
        return def_
    
    def __str__(self):
        return f"ArrayDef(offset={self.first_elem_offset}, used={self.nb_used_entries})"

class ModelData:
    def __init__(self):
        self.file_version = 0
        self.offset_model_data = 0
        self.size_model_data = 0
        self.offset_raw_data = 0
        self.size_raw_data = 0
        self.offset_texture_info = 0
        self.offset_tex_data = 0
        self.size_tex_data = 0

class StaticControllersData:
    def __init__(self):
        self.position = Vector((0.0, 0.0, 0.0))
        self.rotation = Quaternion((1.0, 0.0, 0.0, 0.0))
        self.scale = Vector((1.0, 1.0, 1.0))
        self.alpha = 1.0
        self.self_illum_color = (255, 255, 255, 255)
        self.local_transform = Matrix.Identity(4)
        self.global_transform = Matrix.Identity(4)
        
        # Light-specific properties
        self.light_color = None
        self.light_radius = 10.0
        self.light_shadow_radius = 0.0
        self.light_vertical_displacement = 0.0
        self.light_unknown1 = 0.0
        self.light_unknown2 = 0.0
        self.light_unknown3 = 0.0
    
    def compute_local_transform(self):
        trans_mat = Matrix.Translation(self.position)
        rot_mat = self.rotation.to_matrix().to_4x4()
        scale_mat = Matrix.Diagonal(self.scale).to_4x4()
        self.local_transform = trans_mat @ rot_mat @ scale_mat

class SpeedTreeInstance:
    def __init__(self, tree_type, position, rotation_y):
        self.tree_type = tree_type
        self.position = position
        self.rotation_y = rotation_y

class BoneData:
    def __init__(self):
        self.name = ""
        self.parent = None
        self.local_matrix = Matrix.Identity(4)
        self.global_matrix = Matrix.Identity(4)
        self.children = []
        self.node_id = 0
        self.position = Vector((0, 0, 0))
        self.head = Vector((0, 0, 0))
        self.tail = Vector((0, 0.1, 0))
        self.rotation = Quaternion()

class SkinWeight:
    def __init__(self):
        self.bone_names = []
        self.weights = []

class MaterialParser:
    """Parser for .mat material files"""
    
    def __init__(self):
        self.shader = ""
        self.textures = {}
        self.bumpmaps = {}
        self.strings = {}
        self.vectors = {}
        self.floats = {}
    
    def load_from_string(self, content):
        if not content:
            return
        
        logger.log(f"Parsing material content ({len(content)} chars)")
        
        parts = content.split()
        i = 0
        
        def clean_number_string(s):
            s = s.strip()
            s = s.replace(',', '.')
            if s.count('.') > 1:
                parts = s.split('.')
                if len(parts) > 1:
                    s = parts[0] + '.' + parts[1]
            import re
            match = re.match(r'^-?\d*\.?\d+', s)
            if match:
                s = match.group(0)
            return s
        
        while i < len(parts):
            data = parts[i]
            
            if data == "shader":
                i += 1
                if i < len(parts):
                    self.shader = parts[i]
                    logger.log(f"  Shader: {self.shader}")
            
            elif data == "texture":
                i += 1
                if i + 1 < len(parts):
                    tex_id = parts[i]
                    i += 1
                    tex_name = parts[i]
                    self.textures[tex_id] = tex_name
                    logger.log(f"  Texture: {tex_id} = {tex_name}")
            
            elif data == "bumpmap":
                i += 1
                if i + 1 < len(parts):
                    tex_id = parts[i]
                    i += 1
                    tex_name = parts[i]
                    self.bumpmaps[tex_id] = tex_name
            
            elif data == "string":
                i += 1
                if i + 1 < len(parts):
                    str_id = parts[i]
                    i += 1
                    str_val = parts[i]
                    self.strings[str_id] = str_val
            
            elif data == "float":
                i += 1
                if i + 1 < len(parts):
                    float_id = parts[i]
                    i += 1
                    float_str = clean_number_string(parts[i])
                    try:
                        float_val = float(float_str)
                        self.floats[float_id] = float_val
                    except ValueError:
                        logger.error(f"  Could not convert float value: '{parts[i]}'")
            
            elif data == "vector":
                i += 1
                if i + 4 < len(parts):
                    vec_id = parts[i]
                    i += 1
                    x_str = clean_number_string(parts[i])
                    i += 1
                    y_str = clean_number_string(parts[i])
                    i += 1
                    z_str = clean_number_string(parts[i])
                    if i + 1 < len(parts):
                        w_str = clean_number_string(parts[i + 1])
                    i += 1
                    
                    try:
                        x = float(x_str)
                        y = float(y_str)
                        z = float(z_str)
                        self.vectors[vec_id] = Vector((x, y, z))
                    except ValueError:
                        logger.error(f"  Could not convert vector components")
            
            i += 1
    
    def has_material(self):
        return bool(self.shader) or bool(self.textures)
    
    def get_diffuse_texture(self):
        if not self.textures:
            return ""
        
        for key in ["texture0", "tex", "diffuse_texture", "diffuse_map"]:
            if key in self.textures:
                return self.textures[key]
        
        return next(iter(self.textures.values()))
    
    def get_lightmap_texture(self):
        if "texture1" in self.textures:
            return self.textures["texture1"]
        return ""
    
class BlendTextureParams:
    """Parser for blend_texture_params strings"""
    
    def __init__(self, params_string):
        self.time_texture_map = {}
        self.parse(params_string)
    
    def parse(self, params_string):
        """Parse string format: "6:00-zorza_noc;7:00-zorza_swit;..."""
        if not params_string:
            return
        
        if params_string.startswith("blend_texture_params "):
            params_string = params_string[21:]
        
        entries = params_string.split(';')
        
        for entry in entries:
            if not entry or '-' not in entry:
                continue
            
            time_part, texture = entry.split('-', 1)
            
            # Parse time (format: "HH:MM")
            try:
                if ':' in time_part:
                    hour, minute = map(int, time_part.split(':'))
                else:
                    hour = int(time_part)
                    minute = 0
                
                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    time_float = hour + minute / 60.0
                    self.time_texture_map[time_float] = texture.strip()
            except ValueError:
                continue
    
    def get_texture_for_time(self, time_of_day):
        """Get texture name based on time of day"""
        if not self.time_texture_map:
            return None
        
        target_hour = DEFAULT_HOURS.get(time_of_day, 12)
        
        time_range = TIME_RANGES.get(time_of_day, (0, 23))
        start_hour, end_hour = time_range
        
        matching_textures = []
        
        for time_float, texture in self.time_texture_map.items():
            hour = int(time_float)
            
            if start_hour > end_hour:
                if hour >= start_hour or hour <= end_hour:
                    matching_textures.append((time_float, texture))
            else:
                if start_hour <= hour <= end_hour:
                    matching_textures.append((time_float, texture))
        
        if not matching_textures:
            return None

        matching_textures.sort(key=lambda x: x[0])

        closest_time = None
        closest_texture = None
        min_diff = float('inf')
        
        for time_float, texture in matching_textures:
            diff = abs(time_float - target_hour)
            if diff > 12:
                diff = 24 - diff
            if diff < min_diff:
                min_diff = diff
                closest_time = time_float
                closest_texture = texture
        
        return closest_texture
    
    def has_valid_entries(self):
        return len(self.time_texture_map) > 0

# ============
# MAIN IMPORTER
# ============

class MDBImporter:
    def __init__(self, filepath, game_path=None, time_of_day='DAY', import_speedtrees=False, import_skeletons=False, debug=False):
        self.reader = BinaryReader(filepath)
        self.filepath = filepath
        self.model_data = ModelData()
        self.time_of_day = time_of_day
        self.import_speedtrees = import_speedtrees
        self.import_skeletons = import_skeletons
        self.debug = debug
        
        global logger
        logger.enabled = debug
        
        self.game_root = self._find_game_root(filepath, game_path)
        logger.log(f"Game root path: {self.game_root}")
        
        self.model_name = ""
        self.super_model = ""
        self.model_scale = 1.0
        
        self.meshes = []
        self.speedtree_instances = []
        self.speedtree_types = set()
        
        # Names this model asks for that resolve to nothing, reported once.
        self.unresolved_textures = set()

        self.bones = {}
        self.bone_list = []
        self.root_bones = []
        self.armature_obj = None
    
    def _find_game_root(self, filepath, user_path):
        if user_path and os.path.exists(user_path):
            logger.log(f"Using user-provided game path: {user_path}")
            return user_path
        
        current = os.path.dirname(filepath)
        while current and current != os.path.dirname(current):
            if (os.path.basename(current) in ["meshes00", "data"] or
                os.path.exists(os.path.join(current, "meshes00"))):
                root = current
                if os.path.basename(current) in ["meshes00", "data"]:
                    root = os.path.dirname(current)
                logger.log(f"Found game root: {root}")
                return root
            current = os.path.dirname(current)
        
        fallback = os.path.dirname(filepath)
        logger.log(f"Using fallback path: {fallback}")
        return fallback
    
    def find_texture_file(self, tex_name):
        if not tex_name:
            return None
        
        base_name = tex_name
        if '!' in tex_name:
            base_name = tex_name.split('!')[0]
        
        for folder in TEXTURE_FOLDERS:
            for ext in TEXTURE_EXTENSIONS:
                full_path = os.path.join(self.game_root, folder, tex_name + ext)
                if os.path.exists(full_path):
                    return full_path
                
                full_path = os.path.join(self.game_root, folder, base_name + ext)
                if os.path.exists(full_path):
                    return full_path
        
        return None
    
    def map_texture_name(self, tex_name):
        """Apply special texture name mappings if needed"""
        if not tex_name:
            return tex_name
        
        if tex_name in TEXTURE_NAME_MAPPINGS:
            mapped_name = TEXTURE_NAME_MAPPINGS[tex_name]
            logger.log(f"  '{tex_name}' is not in the game's files; substituting "
                       f"'{mapped_name}'", force=True)
            return mapped_name
        
        return tex_name

    def is_water_shader(self, shader_type):
        water_shaders = ["water_running", "water_still", "water"]
        return shader_type in water_shaders

    def evaluate_time_of_day_texture(self, base_name, day_night_light_maps):
        if not day_night_light_maps or not base_name:
            return base_name
        
        suffix = TIME_OF_DAY_SUFFIXES.get(self.time_of_day, "")
        if not suffix:
            return base_name
        
        test_name = base_name + suffix
        if self.find_texture_file(test_name):
            logger.log(f"  Using time of day texture: {test_name}")
            return test_name
        
        return base_name
    
    def load_material_file(self, mat_filename):
        if not mat_filename:
            return None
        
        logger.log(f"Loading material file: {mat_filename}")
        
        search_paths = [
            os.path.join(self.game_root, "materials00/"),
            os.path.join(self.game_root, "meshes00/"),
            os.path.dirname(self.filepath) + "/",
            self.game_root + "/"
        ]
        
        for base_path in search_paths:
            full_path = os.path.join(base_path, mat_filename + ".mat")
            if os.path.exists(full_path):
                logger.log(f"Found material at: {full_path}")
                try:
                    with open(full_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    parser = MaterialParser()
                    parser.load_from_string(content)
                    return parser
                except Exception as e:
                    logger.error(f"Error loading material: {e}")
        
        return None
    
    def read_shader_from_texture_block(self):
        """Read just the shader name from the texture block without consuming the entire block"""
        current_pos = self.reader.tell()
        
        shader_name = ""
        
        try:
            if self.model_data.file_version == FILE_VERSION_133:
                offset = self.model_data.offset_raw_data + self.model_data.offset_tex_data
            else:
                offset = self.model_data.offset_tex_data + self.model_data.offset_texture_info
            
            self.reader.seek(offset)
            
            texture_count = self.reader.read_u32()
            off_texture = self.reader.read_u32()
            
            for i in range(texture_count):
                line = self.reader.read_string_until_null()
                if line and line.startswith("shader "):
                    shader_name = line[7:].strip()  # Remove "shader " prefix
                    break
        except Exception as e:
            logger.log(f"  Error reading shader from texture block: {e}")

        self.reader.seek(current_pos)
        
        if shader_name:
            logger.log(f"  Found shader in texture block: {shader_name}")
        
        return shader_name
    
    def read_uv_sets(self, t_verts_defs, vertex_count):
        """Read a node's UV sets, each one left in the slot the file gave it."""
        uv_sets = []
        for uv_def in t_verts_defs[:4]:
            if uv_def.nb_used_entries == 0:
                uv_sets.append([])
                continue
            self.reader.seek(self.model_data.offset_raw_data + uv_def.first_elem_offset)
            uvs = []
            for _ in range(uv_def.nb_used_entries):
                u = self.reader.read_f32()
                v = self.reader.read_f32()
                uvs.append((u, 1.0 - v))
            while len(uvs) < vertex_count:
                uvs.append((0.0, 0.0))
            uv_sets.append(uvs)
        while uv_sets and not uv_sets[-1]:
            uv_sets.pop()
        return uv_sets

    def read_normals(self, normals_def, controllers):
        """Read a per-vertex normal array.

        Three signed 16-bit fixed-point components, scale 8192, six bytes per entry.
        """
        normals = []
        if normals_def.nb_used_entries == 0:
            return normals

        rot = controllers.global_transform.to_3x3()
        seek_pos = self.model_data.offset_raw_data + normals_def.first_elem_offset
        self.reader.seek(seek_pos)

        for i in range(normals_def.nb_used_entries):
            x = self.reader.read_s16() / 8192.0
            y = self.reader.read_s16() / 8192.0
            z = self.reader.read_s16() / 8192.0
            n = rot @ Vector((x, y, z))
            if n.length > 1e-8:
                n.normalize()
            else:
                n = Vector((0.0, 0.0, 1.0))
            normals.append(n)

        return normals

    def read_f32_array(self, offset, count):
        if count == 0:
            return []

        pos = self.reader.tell()
        seek_pos = self.model_data.offset_model_data + offset
        self.reader.seek(seek_pos)
        
        array = []
        try:
            for i in range(count):
                array.append(self.reader.read_f32())
        except EOFError:
            logger.error(f"EOF reading f32 array at {seek_pos}")
        
        self.reader.seek(pos)
        return array
    
    def read_u32_array(self, offset, count):
        if count == 0:
            return []
        
        pos = self.reader.tell()
        seek_pos = self.model_data.offset_model_data + offset
        self.reader.seek(seek_pos)
        
        array = []
        try:
            for i in range(count):
                array.append(self.reader.read_u32())
        except EOFError:
            logger.error(f"EOF reading u32 array at {seek_pos}")
        
        self.reader.seek(pos)
        return array
    
    def read_node_controllers(self, key_offset, key_count, data_array, return_all=False, node_type=None):
        if key_count == 0:
            return StaticControllersData()
        
        pos = self.reader.tell()
        seek_pos = self.model_data.offset_model_data + key_offset
        self.reader.seek(seek_pos)
        
        static_data = StaticControllersData()
        
        if node_type == NODE_TYPE_LIGHT:
            static_data.light_radius = 10.0
            static_data.light_shadow_radius = 0.0
            static_data.light_vertical_displacement = 0.0
            static_data.light_unknown1 = 0.0
            static_data.light_unknown2 = 0.0
            static_data.light_unknown3 = 0.0
        
        if return_all:
            anim_data = {
                'position_times': [],
                'positions': [],
                'rotation_times': [],
                'rotations': [],
                'scale_times': [],
                'scales': []
            }
        
        for i in range(key_count):
            try:
                controller_type = self.reader.read_u32()
                row_count = self.reader.read_u16()
                time_index = self.reader.read_u16()
                data_index = self.reader.read_u16()
                column_count = self.reader.read_u8()
                self.reader.seek(1, 1)
                
                if row_count == 0xFFFF:
                    continue
                
                if controller_type == CONTROLLER_POSITION:
                    if column_count == 3 and data_index + 2 < len(data_array):
                        if return_all:
                            for j in range(row_count):
                                time = data_array[time_index + j]
                                pos_x = data_array[data_index + j * 3]
                                pos_y = data_array[data_index + j * 3 + 1]
                                pos_z = data_array[data_index + j * 3 + 2]
                                anim_data['position_times'].append(time)
                                anim_data['positions'].append(Vector((pos_x, pos_y, pos_z)))
                        else:
                            static_data.position = Vector((
                                data_array[data_index],
                                data_array[data_index + 1],
                                data_array[data_index + 2]
                            ))
                            logger.log(f"    Position: {static_data.position}")
                
                elif controller_type == CONTROLLER_ORIENTATION:
                    if column_count == 4 and data_index + 3 < len(data_array):
                        if return_all:
                            for j in range(row_count):
                                time = data_array[time_index + j]
                                x = data_array[data_index + j * 4]
                                y = data_array[data_index + j * 4 + 1]
                                z = data_array[data_index + j * 4 + 2]
                                w = data_array[data_index + j * 4 + 3]
                                anim_data['rotation_times'].append(time)
                                anim_data['rotations'].append(Quaternion((w, x, y, z)))
                        else:

                            x = data_array[data_index]
                            y = data_array[data_index + 1]
                            z = data_array[data_index + 2]
                            w = data_array[data_index + 3]
                            static_data.rotation = Quaternion((w, x, y, z))
                            logger.log(f"    Rotation: ({x}, {y}, {z}, {w})")
                
                elif controller_type == CONTROLLER_SCALE:
                    if data_index < len(data_array):
                        if return_all:
                            for j in range(row_count):
                                time = data_array[time_index + j]
                                scale_val = data_array[data_index + j * column_count]
                                anim_data['scale_times'].append(time)
                                anim_data['scales'].append(Vector((scale_val, scale_val, scale_val)))
                        else:
                            scale_val = data_array[data_index]
                            static_data.scale = Vector((scale_val, scale_val, scale_val))
                            logger.log(f"    Scale: {scale_val}")
                
                elif controller_type == 248:  # Light Color (0xF8)
                    if data_index + 2 < len(data_array):
                        r = data_array[data_index]
                        g = data_array[data_index + 1]
                        b = data_array[data_index + 2]
                        static_data.light_color = (r, g, b)
                        logger.log(f"    Light Color: ({r:.3f}, {g:.3f}, {b:.3f})")
                
                elif controller_type == 260:  # Light Radius (0x104)
                    if data_index < len(data_array):
                        static_data.light_radius = data_array[data_index]
                        logger.log(f"    Light Radius: {static_data.light_radius}")
                
                elif controller_type == 268:  # Light Shadow Radius (0x10C)
                    if data_index < len(data_array):
                        static_data.light_shadow_radius = data_array[data_index]
                        logger.log(f"    Light Shadow Radius: {static_data.light_shadow_radius}")
                
                elif controller_type == 276:  # Light Vertical Displacement (0x114)
                    if data_index < len(data_array):
                        static_data.light_vertical_displacement = data_array[data_index]
                        logger.log(f"    Light Vertical Displacement: {static_data.light_vertical_displacement}")
                
                elif controller_type == 308:  # Light Unknown 1 (0x134)
                    if data_index < len(data_array):
                        static_data.light_unknown1 = data_array[data_index]
                        logger.log(f"    Light Unknown 1: {static_data.light_unknown1}")
                
                elif controller_type == 336:  # Light Unknown 2 (0x150)
                    if data_index < len(data_array):
                        static_data.light_unknown2 = data_array[data_index]
                        logger.log(f"    Light Unknown 2: {static_data.light_unknown2}")
                
                elif controller_type == 340:  # Light Unknown 3 (0x154)
                    if data_index < len(data_array):
                        static_data.light_unknown3 = data_array[data_index]
                        logger.log(f"    Light Unknown 3: {static_data.light_unknown3}")
                
                elif controller_type == CONTROLLER_ALPHA:
                    if data_index < len(data_array):
                        static_data.alpha = data_array[data_index]
                        logger.log(f"    Alpha: {static_data.alpha}")
                
                elif controller_type == CONTROLLER_SELF_ILLUM_COLOR:
                    if node_type != NODE_TYPE_LIGHT and data_index + 2 < len(data_array):
                        r = int(data_array[data_index] * 255)
                        g = int(data_array[data_index + 1] * 255)
                        b = int(data_array[data_index + 2] * 255)
                        static_data.self_illum_color = (r, g, b, 255)
                        logger.log(f"    SelfIllum: ({r},{g},{b})")
            
            except Exception as e:
                logger.error(f"Error reading controller {i}: {e}")
        
        self.reader.seek(pos)
        
        if return_all:
            return anim_data
        else:
            static_data.compute_local_transform()
            return static_data
    
    def read_textures_block(self):
        logger.log(f"Reading textures block at {self.reader.tell()}")
        
        if self.model_data.file_version == FILE_VERSION_133:
            offset = self.model_data.offset_raw_data + self.model_data.offset_tex_data
        else:
            offset = self.model_data.offset_tex_data + self.model_data.offset_texture_info
        
        self.reader.seek(offset)
        
        texture_count = self.reader.read_u32()
        off_texture = self.reader.read_u32()
        logger.log(f"  textureCount={texture_count}, offTexture={off_texture}")
        
        texture_lines = []
        blend_params_string = None
        for i in range(texture_count):
            line = self.reader.read_string_until_null()
            if line:
                texture_lines.append(line)
                logger.log(f"  Line {i}: {line[:50]}...")
                
                if "blend_texture_params" in line:
                    blend_params_string = line
                    logger.log(f"  Found blend_texture_params: {line}")

        blend_params = None
        if blend_params_string and self.time_of_day != 'NONE':
            blend_params = BlendTextureParams(blend_params_string)
            if blend_params.has_valid_entries():
                logger.log(f"  Parsed {len(blend_params.time_texture_map)} blend texture entries")
        
        textures = []
        has_shader_tex = False
        shader_name = ""
        
        shader_index = -1
        for i, line in enumerate(texture_lines):
            if line.startswith("shader "):
                has_shader_tex = True
                shader_name = line[7:].strip()
                shader_index = i
                if shader_name in ["dadd_al_mul_alp", "corona", "normalmap",
                                  "norm_env_rim_ao", "transparency_2ps", "transparency_2p", "skin_n_rim_ao", "trans_cds_2p",
                                  "skin_n_rim_ao_mh", "skin_nrimaoenv", "_default_door", "_default__b", "selfilum", "selfilum_b", "normalmap_env", "spacewarp_glass"]:
                    has_shader_tex = False
                logger.log(f"  Found shader: {shader_name} (consumes slot: {has_shader_tex})")
                break
        
        for line in texture_lines:
            parts = line.split(None, 2)
            if len(parts) < 3 or parts[0] != "texture":
                continue

            slot = DIFFUSE_TEXTURE_KEYS.get(parts[1])
            if slot is None:
                logger.log(f"  Ignoring non-diffuse texture '{parts[1]}'")
                continue

            n = slot + 1 if has_shader_tex else slot
            tex_name = parts[2].strip()
            logger.log(f"  Found {parts[1]} at index {n}: {tex_name}")
            while len(textures) <= n:
                textures.append("")
            textures[n] = tex_name

        if blend_params and blend_params.has_valid_entries() and not textures:
            blend_texture = blend_params.get_texture_for_time(self.time_of_day)
            if blend_texture:
                logger.log(f"  Selected blend texture for {self.time_of_day}: {blend_texture}")
                textures = [blend_texture]
        
        logger.log(f"  Extracted textures: {textures}")
        return textures

    def read_light_node(self, controllers, node_name, node_number):
        """Read a light node and create a Blender light object"""
        logger.log(f"Reading Light node at {self.reader.tell()}")
        
        light_pos = controllers.position.copy()
        
        if hasattr(controllers, 'light_color') and controllers.light_color is not None:
            r, g, b = controllers.light_color
            light_color = (r, g, b)
            logger.log(f"  Light color from controller 248: {light_color}")
        elif hasattr(controllers, 'self_illum_color') and controllers.self_illum_color:
            r, g, b, a = controllers.self_illum_color
            if r > 0 or g > 0 or b > 0:
                light_color = (r / 255.0, g / 255.0, b / 255.0)
                logger.log(f"  Light color from SelfIllum: {light_color}")
            else:
                light_color = (1.0, 0.9, 0.8)
                logger.log(f"  No valid color found, using default: {light_color}")
        else:
            light_color = (1.0, 0.9, 0.8)
            logger.log(f"  No color found, using default: {light_color}")
        
        light_radius = getattr(controllers, 'light_radius', 10.0)
        
        light_shadow_radius = getattr(controllers, 'light_shadow_radius', 0.0)
        light_vertical_displacement = getattr(controllers, 'light_vertical_displacement', 0.0)
        light_unknown1 = getattr(controllers, 'light_unknown1', 0.0)
        light_unknown2 = getattr(controllers, 'light_unknown2', 0.0)
        light_unknown3 = getattr(controllers, 'light_unknown3', 0.0)
        
        logger.log(f"  Light position: {light_pos}")
        logger.log(f"  Light color: {light_color}")
        logger.log(f"  Light radius: {light_radius}")
        
        if self.debug:
            if light_shadow_radius != 0.0:
                logger.log(f"  Light shadow radius: {light_shadow_radius}")
            if light_vertical_displacement != 0.0:
                logger.log(f"  Light vertical displacement: {light_vertical_displacement}")
            if light_unknown1 != 0.0:
                logger.log(f"  Light unknown1: {light_unknown1}")
            if light_unknown2 != 0.0:
                logger.log(f"  Light unknown2: {light_unknown2}")
            if light_unknown3 != 0.0:
                logger.log(f"  Light unknown3: {light_unknown3}")
        
        return {
            'type': 'light',
            'node_name': node_name,
            'node_number': node_number,
            'position': light_pos,
            'color': light_color,
            'energy': 100.0,  # Arbitrary 
            'radius': light_radius,
            'shadow_radius': light_shadow_radius,
            'vertical_displacement': light_vertical_displacement,
            'unknown1': light_unknown1,
            'unknown2': light_unknown2,
            'unknown3': light_unknown3,
            'node_offset': self.reader.tell()
        }

    def create_light_object(self, light_data, collection):
        """Create a Blender light object from light data"""
        node_name = light_data['node_name']
        
        light_data_bl = bpy.data.lights.new(name=node_name, type='POINT')
        light_data_bl.color = light_data['color']
        light_data_bl.energy = light_data['energy']
        light_data_bl.use_shadow = True
        
        if light_data.get('radius', 0) > 0:
            light_data_bl.shadow_soft_size = (light_data['radius'])/10

        # The game's lights illuminate but are never drawn, and a Blender lamp with a
        # radius is a glowing sphere - a white orb on the mirror floors.
        light_data_bl.specular_factor = 0.0
        light_obj = bpy.data.objects.new(name=node_name, object_data=light_data_bl)
        if hasattr(light_obj, 'visible_glossy'):
            light_obj.visible_glossy = False
        light_obj.location = light_data['position']
        
        collection.objects.link(light_obj)
        
        logger.log(f"  Created light: {node_name}")
        logger.log(f"    Position: {light_data['position']}")
        logger.log(f"    Color: {light_data['color']}")
        logger.log(f"    Energy: {light_data['energy']}")
        logger.log(f"    Radius: {light_data.get('radius', 10.0)}")
        
        return light_obj

    def read_trimesh_node(self, controllers):
        """Read a regular trimesh node"""
        logger.log(f"Reading Trimesh node at {self.reader.tell()}")
        
        self.reader.seek(8, 1)
        
        off_mesh_arrays = self.reader.read_u32()
        logger.log(f"  offMeshArrays: 0x{off_mesh_arrays:X}")
        
        self.reader.seek(4, 1)
        
        bounding_min = [self.reader.read_f32() for _ in range(3)]
        bounding_max = [self.reader.read_f32() for _ in range(3)]
        
        self.reader.seek(28, 1)
        
        vol_fog_scale = self.reader.read_f32()
        
        self.reader.seek(16, 1)
        
        diffuse = [self.reader.read_f32() for _ in range(3)]
        ambient = [self.reader.read_f32() for _ in range(3)]
        texture_trans_rot = [self.reader.read_f32() for _ in range(3)]
        
        shininess = self.reader.read_f32()
        
        shadow = self.reader.read_u32() == 1
        beaming = self.reader.read_u32() == 1
        render = self.reader.read_u32() == 1
        
        has_transparency_hint = True
        transparency_hint = self.reader.read_u32() == 1
        
        self.reader.seek(4, 1)
        
        texture_strings = []
        for i in range(4):
            tex = self.reader.read_string(64)
            if tex == "NULL":
                tex = ""
            texture_strings.append(tex)
            if tex:
                logger.log(f"  Texture {i}: {tex}")
        
        tile_fade = self.reader.read_u32() == 1
        
        control_fade = self.reader.read_u8() == 1
        light_mapped = self.reader.read_u8() == 1
        rotate_texture = self.reader.read_u8() == 1
        self.reader.seek(1, 1)
        
        transparency_shift = self.reader.read_f32()
        
        default_render_list = self.reader.read_u32()
        preserve_vcolors = self.reader.read_u32()
        
        four_cc = self.reader.read_u32()
        
        self.reader.seek(4, 1)
        
        depth_offset = self.reader.read_f32()
        corona_center_mult = self.reader.read_f32()
        fade_start_distance = self.reader.read_f32()
        
        dist_from_screen_center_face = self.reader.read_u8() == 1
        self.reader.seek(3, 1)
        
        enlarge_start_distance = self.reader.read_f32()
        
        affected_by_wind = self.reader.read_u8() == 1
        self.reader.seek(3, 1)
        
        damp_factor = self.reader.read_f32()
        
        blend_group = self.reader.read_u32()
        
        day_night_light_maps = self.reader.read_u8() == 1
        
        day_night_transition = self.reader.read_string(200)
        
        ignore_hit_check = self.reader.read_u8() == 1
        needs_reflection = self.reader.read_u8() == 1
        self.reader.seek(1, 1)
        
        reflection_plane_normal = [self.reader.read_f32() for _ in range(3)]
        reflection_plane_distance = self.reader.read_f32()
        
        fade_on_camera_collision = self.reader.read_u8() == 1
        no_self_shadow = self.reader.read_u8() == 1
        is_reflected = self.reader.read_u8() == 1
        only_reflected = self.reader.read_u8() == 1
        
        light_map_name = self.reader.read_string(64)
        if light_map_name == "NULL":
            light_map_name = ""
        logger.log(f"  lightMapName: {light_map_name}")
        
        can_decal = self.reader.read_u8() == 1
        multi_bill_board = self.reader.read_u8() == 1
        ignore_lod_reflection = self.reader.read_u8() == 1
        self.reader.seek(1, 1)
        
        detail_map_scape = self.reader.read_f32()
        
        self.model_data.offset_texture_info = self.reader.read_u32()
        logger.log(f"  offsetTextureInfo: 0x{self.model_data.offset_texture_info:X}")
        
        end_pos = self.reader.tell()
        
        seek_pos = self.model_data.offset_raw_data + off_mesh_arrays
        self.reader.seek(seek_pos)
        
        self.reader.seek(4, 1)
        
        vertex_def = ArrayDef.read(self.reader)
        normals_def = ArrayDef.read(self.reader)
        tangents_def = ArrayDef.read(self.reader)
        binormals_def = ArrayDef.read(self.reader)
        
        t_verts_defs = []
        for t in range(4):
            t_verts_defs.append(ArrayDef.read(self.reader))
        
        unknown_def = ArrayDef.read(self.reader)
        faces_def = ArrayDef.read(self.reader)
        
        if self.model_data.file_version == FILE_VERSION_133:
            self.model_data.offset_tex_data = self.reader.read_u32()
            logger.log(f"  offsetTexData: 0x{self.model_data.offset_tex_data:X}")
        
        logger.log(f"  Vertices: {vertex_def.nb_used_entries}, Faces: {faces_def.nb_used_entries}")
        
        if vertex_def.nb_used_entries == 0 or faces_def.nb_used_entries == 0:
            self.reader.seek(end_pos)
            return None
        
        # Read vertices
        vertices = []
        seek_pos = self.model_data.offset_raw_data + vertex_def.first_elem_offset
        self.reader.seek(seek_pos)
        for i in range(vertex_def.nb_used_entries):
            x = self.reader.read_f32()
            y = self.reader.read_f32()
            z = self.reader.read_f32()
            v = controllers.global_transform @ Vector((x, y, z))
            vertices.append(v)
        
        # Read normals
        normals = self.read_normals(normals_def, controllers)
        
        # Read embedded textures block
        embedded_textures = self.read_textures_block()
        logger.log(f"  Extracted textures: {embedded_textures}")
        
        shader_type = ""

        shader_type = self.read_shader_from_texture_block()

        water_params = {}
        if self.is_water_shader(shader_type):
            if self.model_data.file_version == FILE_VERSION_133:
                offset = self.model_data.offset_raw_data + self.model_data.offset_tex_data
            else:
                offset = self.model_data.offset_tex_data + self.model_data.offset_texture_info
            
            current_pos = self.reader.tell()
            self.reader.seek(offset)
            
            texture_count = self.reader.read_u32()
            off_texture = self.reader.read_u32()
            
            for i in range(texture_count):
                line = self.reader.read_string_until_null()
                if line:
                    parts = line.split()
                    if len(parts) >= 2:
                        if parts[0] == "vector" and parts[1] == "water_color" and len(parts) >= 6:
                            try:
                                water_params['water_color'] = (
                                    float(parts[2].replace(',', '.')),
                                    float(parts[3].replace(',', '.')),
                                    float(parts[4].replace(',', '.')),
                                    float(parts[5].replace(',', '.'))
                                )
                            except ValueError:
                                pass
                        elif parts[0] == "texture" and parts[1] == "depth_texture" and len(parts) >= 3:
                            water_params['depth_texture'] = parts[2]
                        elif parts[0] == "texture" and parts[1] == "bump_texture" and len(parts) >= 3:
                            water_params['bump_texture'] = parts[2]
            
            self.reader.seek(current_pos)
            
            logger.log(f"  Water parameters extracted: {water_params}")
        
        normal_map_texture = None
        normal_map_uv_index = 0
        
        bumpmap_texture = None

        textures_to_use = []
        texture_uv_indices = []
        
        lightmap_texture = None
        lightmap_uv_index = -1
        
        # Check if we have a lightmap
        if light_map_name and self.time_of_day != 'NONE' and day_night_light_maps:
            lightmap_tex = self.evaluate_time_of_day_texture(light_map_name, day_night_light_maps)
            if lightmap_tex and self.find_texture_file(lightmap_tex):
                for uv_idx in range(4):
                    if uv_idx < len(t_verts_defs) and t_verts_defs[uv_idx].nb_used_entries > 0:
                        lightmap_texture = lightmap_tex
                        lightmap_uv_index = uv_idx
                        logger.log(f"  Found lightmap: {lightmap_tex} (using UV{uv_idx})")
                        break
        
        # First, check for material file reference
        material_file_uv_index = -1
        material_params = None
        if len(texture_strings) > 0 and texture_strings[0] == "_shader_" and len(texture_strings) > 1 and texture_strings[1]:
            # The material's name sits in slot 1, which says nothing about the UV set its
            # textures sample.
            material_file_uv_index = 1 if (len(t_verts_defs) > 1
                                           and t_verts_defs[1].nb_used_entries > 0) else 0
            logger.log(f"  Material file reference: {texture_strings[1]} (UV index {material_file_uv_index})")
            mat_parser = self.load_material_file(texture_strings[1])
            if mat_parser and mat_parser.has_material():
                # Keep the whole material file, not just its diffuse: some shaders name several
                # textures and the speeds their matrices scroll at.
                material_params = {
                    'name': texture_strings[1],
                    'shader': mat_parser.shader,
                    'textures': dict(mat_parser.textures),
                    'floats': dict(mat_parser.floats),
                    'uv_index': material_file_uv_index,
                }
                diffuse = mat_parser.get_diffuse_texture()
                if diffuse:
                    textures_to_use.append(diffuse)
                    texture_uv_indices.append(material_file_uv_index)
                    logger.log(f"  Diffuse from material file: {diffuse} (using UV{material_file_uv_index})")
        
        # If no material file, try embedded textures for diffuse
        if not textures_to_use:
            for i, tex in enumerate(embedded_textures):
                uv_slot = uv_slot_for(t_verts_defs, i) if tex else -1
                if uv_slot >= 0:
                    mapped_tex = self.map_texture_name(tex)
                    if mapped_tex != tex:
                        tex = mapped_tex
                    
                    if lightmap_texture and (tex == light_map_name or tex == lightmap_texture):
                        continue
                    textures_to_use.append(tex)
                    texture_uv_indices.append(uv_slot)
                    logger.log(f"  Diffuse from embedded[{i}]: {tex} (UV{uv_slot})")
        
        # Then try static textures from node for diffuse
        if not textures_to_use:
            for i, tex in enumerate(texture_strings):
                uv_slot = uv_slot_for(t_verts_defs, i) if (tex and tex != "NULL") else -1
                if uv_slot >= 0:
                    mapped_tex = self.map_texture_name(tex)
                    if mapped_tex != tex:
                        tex = mapped_tex
                    
                    if lightmap_texture and (tex == light_map_name or tex == lightmap_texture):
                        continue
                    if tex not in textures_to_use:
                        textures_to_use.append(tex)
                        texture_uv_indices.append(uv_slot)
                        logger.log(f"  Diffuse from static[{i}]: {tex} (UV{uv_slot})")
        
        # Now add the lightmap - as the first texture (index 0) so it becomes the top/overlay
        if lightmap_texture and lightmap_uv_index >= 0:
            textures_to_use.insert(0, lightmap_texture)
            texture_uv_indices.insert(0, lightmap_uv_index)
            logger.log(f"  Lightmap added as primary texture: {lightmap_texture} (using UV{lightmap_uv_index})")
        
        valid_textures = []
        valid_indices = []
        for i, tex in enumerate(textures_to_use):
            if tex and self.find_texture_file(tex):
                valid_textures.append(tex)
                valid_indices.append(texture_uv_indices[i])
            else:
                self.unresolved_textures.add(tex)
                logger.log(f"  Texture not found, skipping: {tex}")
        
        # If we still have no textures but have a lightmap, use just the lightmap
        if not valid_textures and lightmap_texture and self.find_texture_file(lightmap_texture):
            valid_textures = [lightmap_texture]
            valid_indices = [lightmap_uv_index]
            logger.log(f"  Using only lightmap: {lightmap_texture}")
        
        logger.log(f"  Final textures: {valid_textures}")
        logger.log(f"  UV indices: {valid_indices}")
        
        uv_sets = self.read_uv_sets(t_verts_defs, vertex_def.nb_used_entries)

        # Read faces
        indices = []
        seek_pos = self.model_data.offset_raw_data + faces_def.first_elem_offset
        self.reader.seek(seek_pos)
        
        logger.log(f"  Reading {faces_def.nb_used_entries} faces at 0x{seek_pos:X}")
        
        for i in range(faces_def.nb_used_entries):
            try:
                self.reader.seek(4 * 4 + 4, 1)
                
                if self.model_data.file_version == FILE_VERSION_133:
                    self.reader.seek(3 * 4, 1)
                
                i1 = self.reader.read_u32()
                i2 = self.reader.read_u32()
                i3 = self.reader.read_u32()

                indices.extend([i1, i2, i3])

                if self.model_data.file_version == FILE_VERSION_133:
                    self.reader.seek(4, 1)
                
            except EOFError:
                logger.error(f"EOF reading face {i}")
                break
        
        self.reader.seek(end_pos)
        
        is_transparent = transparency_hint or (controllers.alpha < 1.0)
        
        return {
            'vertices': vertices,
            'normals': normals if normals else [],
            'uv_sets': uv_sets,
            'uv_indices': valid_indices,
            'indices': indices,
            'textures': valid_textures,
            # Which of those textures is the lightmap, by name. The material
            # builder used to take the first one on faith, which only held
            # because the lightmap happens to be inserted at the front.
            'lightmap_texture': resolve_lightmap_name(lightmap_texture, light_map_name,
                                                      valid_textures),
            'alpha': controllers.alpha if controllers.alpha < 1.0 else None,
            'is_transparent': is_transparent,
            'transparency_hint': transparency_hint,
            'render_pass': four_cc,
            'material_params': material_params,
            'needs_reflection': needs_reflection,
            'reflection_plane_normal': reflection_plane_normal,
            'reflection_plane_distance': reflection_plane_distance,
            'diffuse': diffuse,
            'ambient': ambient,
            'shininess': shininess,
            'node_name': '',
            'node_offset': 0,
            'node_number': 0,
            'day_night_light_maps': day_night_light_maps,
            'light_map_name': light_map_name,
            'shader_type': shader_type,
            'water_params': water_params if self.is_water_shader(shader_type) else None            
        }
    
    def read_texture_paint_node(self, controllers):
        logger.log(f"Reading TexturePaint node at {self.reader.tell()}")
        
        layers_def = ArrayDef.read(self.reader)
        
        self.reader.seek(28, 1)
        off_mesh_arrays = self.reader.read_u32()
        logger.log(f"  offMeshArrays: 0x{off_mesh_arrays:X}")
        
        sector_ids = [self.reader.read_u32() for _ in range(4)]
        
        bounding_min = [self.reader.read_f32() for _ in range(3)]
        bounding_max = [self.reader.read_f32() for _ in range(3)]
        
        diffuse = [self.reader.read_f32() for _ in range(3)]
        ambient = [self.reader.read_f32() for _ in range(3)]
        texture_trans_rot = [self.reader.read_f32() for _ in range(3)]
        
        shadow = self.reader.read_u32() == 1
        render = self.reader.read_u32() == 1
        
        tile_fade = self.reader.read_u32() == 1
        
        control_fade = self.reader.read_u8() == 1
        light_mapped = self.reader.read_u8() == 1
        rotate_texture = self.reader.read_u8() == 1
        self.reader.seek(1, 1)
        
        transparency_shift = self.reader.read_f32()
        
        default_render_list = self.reader.read_u32()
        four_cc = self.reader.read_u32()
        
        self.reader.seek(4, 1)
        
        depth_offset = self.reader.read_f32()
        blend_group = self.reader.read_u32()
        
        day_night_light_maps = self.reader.read_u8() == 1
        day_night_transition = self.reader.read_string(200)
        
        ignore_hit_check = self.reader.read_u8() == 1
        needs_reflection = self.reader.read_u8() == 1
        self.reader.seek(1, 1)
        
        reflection_plane_normal = [self.reader.read_f32() for _ in range(3)]
        reflection_plane_distance = self.reader.read_f32()
        
        fade_on_camera_collision = self.reader.read_u8() == 1
        no_self_shadow = self.reader.read_u8() == 1
        is_reflected = self.reader.read_u8() == 1
        self.reader.seek(1, 1)
        
        detail_map_scape = self.reader.read_f32()
        
        only_reflected = self.reader.read_u8() == 1
        light_map_name = self.reader.read_string(64)
        if light_map_name == "NULL":
            light_map_name = ""
        
        can_decal = self.reader.read_u8() == 1
        ignore_lod_reflection = self.reader.read_u8() == 1
        enable_specular = self.reader.read_u8() == 1
        
        end_pos = self.reader.tell()
        
        seek_pos = self.model_data.offset_raw_data + off_mesh_arrays
        self.reader.seek(seek_pos)
        
        self.reader.seek(4, 1)
        
        vertex_def = ArrayDef.read(self.reader)
        normals_def = ArrayDef.read(self.reader)
        tangents_def = ArrayDef.read(self.reader)
        binormals_def = ArrayDef.read(self.reader)
        
        t_verts_defs = []
        for t in range(4):
            t_verts_defs.append(ArrayDef.read(self.reader))
        
        unknown_def = ArrayDef.read(self.reader)
        faces_def = ArrayDef.read(self.reader)
        
        logger.log(f"  Vertices: {vertex_def.nb_used_entries}, Faces: {faces_def.nb_used_entries}")
        
        if vertex_def.nb_used_entries == 0 or faces_def.nb_used_entries == 0:
            self.reader.seek(end_pos)
            return None
        
        vertices = []
        seek_pos = self.model_data.offset_raw_data + vertex_def.first_elem_offset
        self.reader.seek(seek_pos)
        for i in range(vertex_def.nb_used_entries):
            x = self.reader.read_f32()
            y = self.reader.read_f32()
            z = self.reader.read_f32()
            v = controllers.global_transform @ Vector((x, y, z))
            vertices.append(v)
        
        normals = self.read_normals(normals_def, controllers)
        
        all_uv_sets = []
        for uv_def in t_verts_defs:
            uvs = []
            if uv_def and uv_def.nb_used_entries > 0:
                seek_pos = self.model_data.offset_raw_data + uv_def.first_elem_offset
                self.reader.seek(seek_pos)
                for i in range(uv_def.nb_used_entries):
                    u = self.reader.read_f32()
                    v = self.reader.read_f32()
                    uvs.append((u, 1.0 - v))
            all_uv_sets.append(uvs)

        # Slot 0 is this node's place in the level's lightmap atlas, slot 1 the terrain's
        # own tiled paint mapping.
        lightmap_uvs = list(all_uv_sets[0]) if all_uv_sets else []
        base_uvs = []
        for candidate in all_uv_sets[1:]:
            if candidate:
                base_uvs = list(candidate)
                break
        if not base_uvs:
            base_uvs = list(lightmap_uvs)
        
        while len(base_uvs) < len(vertices):
            base_uvs.append((0.0, 0.0))
        while len(lightmap_uvs) < len(vertices):
            lightmap_uvs.append((0.0, 0.0))
        
        layers = []
        pos = self.reader.tell()
        
        for layer_idx in range(layers_def.nb_used_entries):
            seek_pos = self.model_data.offset_raw_data + layers_def.first_elem_offset + (layer_idx * 52)
            self.reader.seek(seek_pos)
            
            has_texture = self.reader.read_u8() == 1
            self.reader.seek(3, 1)
            self.reader.seek(4, 1)

            texture_name = self.reader.read_string(32)
            if texture_name == "NULL":
                texture_name = ""
            
            weights_def = ArrayDef.read(self.reader)
            
            logger.log(f"  Layer {layer_idx}: hasTexture={has_texture}, texture={texture_name}, weights={weights_def.nb_used_entries}")
            
            if weights_def.nb_used_entries > 0:
                weights = []
                weights_pos = self.reader.tell()
                weights_seek = self.model_data.offset_raw_data + weights_def.first_elem_offset
                self.reader.seek(weights_seek)
                
                for w in range(weights_def.nb_used_entries):
                    weight = self.reader.read_f32()
                    weights.append(weight)
                
                self.reader.seek(weights_pos)
                
                if texture_name == light_map_name and day_night_light_maps:
                    texture_name = self.evaluate_time_of_day_texture(texture_name, day_night_light_maps)

                if not has_texture:
                    texture_name = ""

                layers.append({
                    'texture': texture_name,
                    'weights': weights
                })
        
        self.reader.seek(pos)
        
        indices = []
        seek_pos = self.model_data.offset_raw_data + faces_def.first_elem_offset
        self.reader.seek(seek_pos)
        
        for i in range(faces_def.nb_used_entries):
            try:
                i1 = self.reader.read_u32()
                i2 = self.reader.read_u32()
                i3 = self.reader.read_u32()
                
                indices.extend([i1, i2, i3])
                
                self.reader.seek(68, 1)
            except EOFError:
                logger.error(f"EOF reading face {i}")
                break
        
        self.reader.seek(end_pos)
        
        lightmap_texture = ""
        if light_map_name and self.time_of_day != 'NONE':
            lightmap_texture = self.evaluate_time_of_day_texture(light_map_name, day_night_light_maps)
            logger.log(f"  Lightmap: {lightmap_texture}")
        
        return {
            'vertices': vertices,
            'normals': normals,
            'base_uvs': base_uvs,
            'layers': layers,
            'indices': indices,
            'lightmap_texture': lightmap_texture,
            'lightmap_uvs': lightmap_uvs,
            'alpha': controllers.alpha if controllers.alpha < 1.0 else None,
            'is_transparent': False,
            'diffuse': diffuse,
            'ambient': ambient,
            'day_night_light_maps': day_night_light_maps,
            'light_map_name': light_map_name,
            'is_texture_paint': True,
            'node_name': '',
            'node_offset': 0,
            'node_number': 0
        }
    
    def read_skin_node(self, controllers, bone_node=None):
        logger.log(f"Reading Skin node at {self.reader.tell()}")

        # A skin node opens with the same 148-byte header a trimesh node does,
        # so the transparency hint sits at the same place and is worth reading
        # rather than skipping: it is how the file says a mesh needs blending.
        self.reader.seek(140, 1)
        transparency_hint = self.reader.read_u32() == 1
        self.reader.seek(4, 1)
        
        texture_strings = []
        for i in range(4):
            tex = self.reader.read_string(64)
            if tex == "NULL":
                tex = ""
            texture_strings.append(tex)
            if tex:
                logger.log(f"  Skin texture {i}: {tex}")
        
        # Same 61-byte run a trimesh node has between its texture names and the
        # day/night string, so the render list and the render-pass tag sit at
        # the same offsets here too.
        self.reader.seek(12, 1)
        default_render_list = self.reader.read_u32()
        self.reader.seek(4, 1)
        four_cc = self.reader.read_u32()
        self.reader.seek(37, 1)

        day_night_transition = self.reader.read_string(200)
        
        self.reader.seek(2 + 1 + 12 + 8, 1)
        
        light_map_name = self.reader.read_string(64)
        if light_map_name == "NULL":
            light_map_name = ""
        
        self.reader.seek(8, 1)
        
        self.model_data.offset_texture_info = self.reader.read_u32()
        logger.log(f"  offsetTextureInfo: 0x{self.model_data.offset_texture_info:X}")
        
        self.reader.seek(4, 1)
        
        bones_infos = ArrayDef.read(self.reader)
        logger.log(f"  Bones: {bones_infos.nb_used_entries}")
        
        bone_names = []
        if bones_infos.nb_used_entries > 0:
            pos = self.reader.tell()
            seek_pos = self.model_data.offset_tex_data + bones_infos.first_elem_offset
            self.reader.seek(seek_pos)
            for i in range(bones_infos.nb_used_entries):
                bone_id = self.reader.read_u32()
                bone_name = self.reader.read_string(92)
                bone_names.append(bone_name)
                logger.log(f"    Bone {i}: {bone_name} (ID: {bone_id})")
            self.reader.seek(pos)
        
        self.reader.seek(4, 1)
        
        vertex_def = ArrayDef.read(self.reader)
        normals_def = ArrayDef.read(self.reader)
        tangents_def = ArrayDef.read(self.reader)
        binormals_def = ArrayDef.read(self.reader)
        
        t_verts_defs = []
        for t in range(4):
            t_verts_defs.append(ArrayDef.read(self.reader))
        
        unknown_def = ArrayDef.read(self.reader)
        faces_def = ArrayDef.read(self.reader)
        
        if self.model_data.file_version == FILE_VERSION_133:
            self.model_data.offset_tex_data = self.reader.read_u32()
            logger.log(f"  offsetTexData: 0x{self.model_data.offset_tex_data:X}")
        
        logger.log(f"  Vertices: {vertex_def.nb_used_entries}, Faces: {faces_def.nb_used_entries}")
        
        if vertex_def.nb_used_entries == 0 or faces_def.nb_used_entries == 0:
            return None
        
        self.reader.seek(24 + 12, 1)
        weighting_def = ArrayDef.read(self.reader)
        bones_def = ArrayDef.read(self.reader)
        
        skin_weights = []
        if weighting_def.nb_used_entries > 0:
            seek_pos = self.model_data.offset_raw_data + weighting_def.first_elem_offset
            self.reader.seek(seek_pos)
            for i in range(weighting_def.nb_used_entries):
                weight = self.reader.read_f32()
                skin_weights.append(weight)
            logger.log(f"  Read {len(skin_weights)} skinning weights")
        
        bone_indices = []
        if bones_def.nb_used_entries > 0:
            seek_pos = self.model_data.offset_raw_data + bones_def.first_elem_offset
            self.reader.seek(seek_pos)
            for i in range(bones_def.nb_used_entries):
                bone_idx = self.reader.read_u8()
                bone_indices.append(bone_idx)
            logger.log(f"  Read {len(bone_indices)} bone indices")
        
        vertices = []
        seek_pos = self.model_data.offset_raw_data + vertex_def.first_elem_offset
        self.reader.seek(seek_pos)
        for i in range(vertex_def.nb_used_entries):
            x = self.reader.read_f32()
            y = self.reader.read_f32()
            z = self.reader.read_f32()
            v = controllers.global_transform @ Vector((x, y, z))
            vertices.append(v)
        
        normals = self.read_normals(normals_def, controllers)
        
        vertex_weights = []
        weight_index = 0
        if self.import_skeletons:
            influences_per_vertex = 4
            total_influences = vertex_def.nb_used_entries * influences_per_vertex
            
            logger.log(f"  Processing skinning data: {vertex_def.nb_used_entries} vertices, {len(skin_weights)} weights, {len(bone_indices)} indices")
            
            for v_idx in range(vertex_def.nb_used_entries):
                vert_weights = []
                for j in range(influences_per_vertex):
                    if weight_index < len(bone_indices):
                        bone_idx = bone_indices[weight_index]
                        if bone_idx != 255 and bone_idx < len(bone_names):
                            weight = skin_weights[weight_index] if weight_index < len(skin_weights) else 0.0
                            if weight > 0.001:
                                bone_name = bone_names[bone_idx]
                                vert_weights.append((bone_name, weight))
                    weight_index += 1
                
                total_weight = sum(w for _, w in vert_weights)
                if total_weight > 0 and abs(total_weight - 1.0) > 0.001:
                    vert_weights = [(bone, w/total_weight) for bone, w in vert_weights]
                
                vertex_weights.append(vert_weights)
            
            logger.log(f"  Created skinning data for {len(vertex_weights)} vertices")
            logger.log(f"  Total influences processed: {weight_index}")
        else:
            logger.log(f"  Skipping skinning data processing (skeleton import disabled)")
            vertex_weights = []    
            
        skinned_verts = sum(1 for w in vertex_weights if w)
        logger.log(f"  Vertices with skinning: {skinned_verts}/{len(vertex_weights)}")
        
        embedded_textures = self.read_textures_block()
        
        shader_type = self.read_shader_from_texture_block()
        logger.log(f"  Skin shader type: {shader_type}")
        
        # First, read the texture block to get the shader info
        shader_consumes_slot = False
        shader_name = ""

        if self.model_data.file_version == FILE_VERSION_133:
            offset = self.model_data.offset_raw_data + self.model_data.offset_tex_data
        else:
            offset = self.model_data.offset_tex_data + self.model_data.offset_texture_info
        
        current_pos = self.reader.tell()
        self.reader.seek(offset)
        
        texture_count = self.reader.read_u32()
        off_texture = self.reader.read_u32()
        
        # Read first line to check for shader
        first_line = self.reader.read_string_until_null()
        if first_line and first_line.startswith("shader "):
            shader_name = first_line[7:].strip()
            non_consuming_shaders = ["dadd_al_mul_alp", "corona", "normalmap", 
                                    "norm_env_rim_ao", "transparency_2ps", "skin_n_rim_ao", "trans_cds_2p",
                                    "skin_n_rim_ao_mh", "skin_nrimaoenv",
                                    "_default__b", "selfilum", "selfilum_b", "normalmap_env", 
                                    "spacewarp_glass"]
            if shader_name not in non_consuming_shaders:
                shader_consumes_slot = True
        
        self.reader.seek(current_pos)
        
        logger.log(f"  Skin shader: {shader_name}, consumes slot: {shader_consumes_slot}")
        
        textures_to_use = []
        texture_uv_indices = []
        lightmap_texture = None
        
        # First, check for material file reference
        if len(texture_strings) > 0 and texture_strings[0] == "_shader_" and len(texture_strings) > 1 and texture_strings[1]:
            logger.log(f"  Skin material file reference: {texture_strings[1]}")
            mat_parser = self.load_material_file(texture_strings[1])
            if mat_parser and mat_parser.has_material():
                diffuse = mat_parser.get_diffuse_texture()
                if diffuse:
                    textures_to_use.append(diffuse)
                    texture_uv_indices.append(0)
                
                if self.time_of_day != 'NONE':
                    lightmap = mat_parser.get_lightmap_texture()
                    if lightmap:
                        textures_to_use.append(lightmap)
                        texture_uv_indices.append(1)
                        lightmap_texture = lightmap
        else:
            # Process embedded textures
            for i, tex in enumerate(embedded_textures):
                if not tex:
                    continue
                
                uv_index = uv_slot_for(t_verts_defs, i + 1 if shader_consumes_slot else i)
                if uv_index >= 0:
                    if tex == light_map_name and self.time_of_day != 'NONE':
                        tex = self.evaluate_time_of_day_texture(tex, True)
                        if tex:
                            textures_to_use.append(tex)
                            texture_uv_indices.append(uv_index)
                            lightmap_texture = tex
                            logger.log(f"  Lightmap from embedded[{i}] -> UV{uv_index}: {tex}")
                    elif tex:
                        textures_to_use.append(tex)
                        texture_uv_indices.append(uv_index)
                        logger.log(f"  Diffuse from embedded[{i}] -> UV{uv_index}: {tex}")
            
            # Process static texture strings as fallback
            for i, tex in enumerate(texture_strings):
                if not tex or tex == "NULL":
                    continue
                
                if tex in textures_to_use:
                    continue
                
                uv_index = uv_slot_for(t_verts_defs, i + 1 if shader_consumes_slot else i)
                if uv_index >= 0:
                    if tex == light_map_name and self.time_of_day != 'NONE':
                        tex = self.evaluate_time_of_day_texture(tex, True)
                        if tex and tex not in textures_to_use:
                            textures_to_use.append(tex)
                            texture_uv_indices.append(uv_index)
                            lightmap_texture = tex
                            logger.log(f"  Lightmap from static[{i}] -> UV{uv_index}: {tex}")
                    elif tex and tex not in textures_to_use:
                        textures_to_use.append(tex)
                        texture_uv_indices.append(uv_index)
                        logger.log(f"  Diffuse from static[{i}] -> UV{uv_index}: {tex}")
        
        valid_textures = []
        valid_indices = []
        for i, tex in enumerate(textures_to_use):
            if tex and self.find_texture_file(tex):
                valid_textures.append(tex)
                valid_indices.append(texture_uv_indices[i])
            else:
                self.unresolved_textures.add(tex)
                logger.log(f"  Texture not found, skipping: {tex}")
        
        # If no valid textures found, try fallback using first embedded texture
        if not valid_textures and embedded_textures:
            for i, tex in enumerate(embedded_textures):
                if tex and self.find_texture_file(tex):
                    valid_textures.append(tex)
                    valid_indices.append(0)
                    logger.log(f"  Fallback: using embedded texture[{i}]: {tex} (forced UV0)")
                    break
        
        logger.log(f"  Skin final textures: {valid_textures}")
        logger.log(f"  UV indices: {valid_indices}")
        
        uv_sets = self.read_uv_sets(t_verts_defs, vertex_def.nb_used_entries)

        # Read faces
        indices = []
        seek_pos = self.model_data.offset_raw_data + faces_def.first_elem_offset
        self.reader.seek(seek_pos)
        
        for i in range(faces_def.nb_used_entries):
            try:
                self.reader.seek(4 * 4 + 4, 1)
                
                if self.model_data.file_version == FILE_VERSION_133:
                    self.reader.seek(3 * 4, 1)
                
                i1 = self.reader.read_u32()
                i2 = self.reader.read_u32()
                i3 = self.reader.read_u32()
                
                indices.extend([i1, i2, i3])
                
                if self.model_data.file_version == FILE_VERSION_133:
                    self.reader.seek(4, 1)
            
            except EOFError:
                logger.error(f"EOF reading face {i}")
                break
        
        return {
            'vertices': vertices,
            'normals': normals,
            'uv_sets': uv_sets,
            'uv_indices': valid_indices,
            'indices': indices,
            'textures': valid_textures,
            'lightmap_texture': resolve_lightmap_name(lightmap_texture, light_map_name,
                                                      valid_textures),
            'alpha': controllers.alpha if controllers.alpha < 1.0 else None,
            'is_transparent': transparency_hint or controllers.alpha < 1.0,
            'transparency_hint': transparency_hint,
            'render_pass': four_cc,
            'node_name': '',
            'node_offset': 0,
            'node_number': 0,
            'shader_type': shader_type,
            'skin_weights': vertex_weights if self.import_skeletons else [],
            'bone_node': bone_node if self.import_skeletons else None 
        }
    
    def read_speedtree_node(self, controllers, node_name, node_number):
        """Read a SpeedTree node and store its instance data"""
        logger.log(f"Reading SpeedTree node at {self.reader.tell()}")
        
        actual_tree_name = node_name
        
        # First, check if it's in the pattern "something_speedtreeXX"
        speedtree_match = re.search(r'^(.*?)speedtree(\d+)$', node_name, re.IGNORECASE)
        if speedtree_match:
            # This captures: ob_bush + speedtree + 14 -> ob_bush14
            base_name = speedtree_match.group(1)
            tree_number = speedtree_match.group(2)
            actual_tree_name = base_name + tree_number
            logger.log(f"  Stripped 'speedtree' from name: '{node_name}' -> '{actual_tree_name}'")
        else:
            # Try bracket extraction as fallback
            bracket_match = re.search(r'\[([^\]]+)\]', node_name)
            if bracket_match:
                actual_tree_name = bracket_match.group(1)
                logger.log(f"  Extracted tree name from brackets: '{actual_tree_name}'")
            else:
                # Remove trailing numbers as last resort
                actual_tree_name = re.sub(r'_\d+$', '', node_name)
        
        self.speedtree_types.add(actual_tree_name)
        
        world_pos = controllers.global_transform.to_translation()
        world_rot = controllers.global_transform.to_euler('XYZ')
        
        self.speedtree_instances.append(SpeedTreeInstance(
            actual_tree_name,
            world_pos,
            world_rot
        ))
        
        logger.log(f"  Added SpeedTree instance: {actual_tree_name} at {world_pos}")
        
        return None
    
    def load_node(self, parent_matrix=Matrix.Identity(4), node_offset=None, parent_bone=None):
        node_pos = self.reader.tell()
        if node_offset is None:
            node_offset = node_pos
        try:
            self.reader.seek(24, 1)
            
            inherit_color = self.reader.read_u32()
            node_number = self.reader.read_u32()
            
            node_name = self.reader.read_string(64)
                       
            should_skip = False
            node_name_lower = node_name.lower()
            for pattern in SKIP_NODE_PATTERNS:
                if pattern in node_name_lower:
                    should_skip = True
                    logger.log_node(node_name, 0, f"Skipping node '{node_name}' (matches pattern '{pattern}')")
                    break
            
            if should_skip:
                return []
            
            self.reader.seek(8, 1)
            
            children_def = ArrayDef.read(self.reader)
            children = self.read_u32_array(children_def.first_elem_offset, children_def.nb_used_entries)
            
            controller_key_def = ArrayDef.read(self.reader)
            controller_data_def = ArrayDef.read(self.reader)
            
            controller_data = self.read_f32_array(
                controller_data_def.first_elem_offset,
                controller_data_def.nb_used_entries
            )
            
            self.reader.seek(4, 1)
            
            imposter_group = self.reader.read_u32()
            fixed_rot = self.reader.read_u32()
            
            min_lod = self.reader.read_s32()
            max_lod = self.reader.read_s32()
            
            node_type = self.reader.read_u32()
            
            logger.log_node(node_name, node_type, f"Loading node at 0x{node_pos:X}")
            logger.push()
            logger.log_node(node_name, node_type, f"Node: '{node_name}' (ID: {node_number})")
            logger.log_node(node_name, node_type, f"  Type: 0x{node_type:X}, LOD: {min_lod}-{max_lod}")
            
            controllers = self.read_node_controllers(
                controller_key_def.first_elem_offset,
                controller_key_def.nb_used_entries,
                controller_data,
                return_all=False,
                node_type=node_type
            )
            controllers.global_transform = parent_matrix @ controllers.local_transform
            
            bone = None
            if node_name and node_name != "NULL" and self.import_skeletons:
                bone = BoneData()
                bone.name = node_name
                bone.node_id = node_number
                bone.local_matrix = controllers.local_transform
                bone.global_matrix = controllers.global_transform
                bone.parent = parent_bone
                
                bone.position = controllers.global_transform.to_translation()
                
                self.bones[node_name] = bone
                if parent_bone:
                    parent_bone.children.append(bone)
                else:
                    self.root_bones.append(bone)
                self.bone_list.append(bone)
                
                logger.log_node(node_name, node_type, f"  Created bone: {node_name} at {bone.position}")
            
            node_data = None
            
            if min_lod == 0 or min_lod == -1:
                if node_type == NODE_TYPE_TRIMESH:
                    node_data = self.read_trimesh_node(controllers)
                elif node_type == NODE_TYPE_TEXTURE_PAINT:
                    node_data = self.read_texture_paint_node(controllers)
                elif node_type == NODE_TYPE_SKIN:
                    node_data = self.read_skin_node(controllers, bone)
                elif node_type == NODE_TYPE_SPEEDTREE and self.import_speedtrees:
                    node_data = self.read_speedtree_node(controllers, node_name, node_number)
                elif node_type == NODE_TYPE_LIGHT:
                    node_data = self.read_light_node(controllers, node_name, node_number)
                elif node_type == NODE_TYPE_EMITTER:
                    # Particle emitters carry no geometry this importer can
                    # rebuild, but their placement is the useful part, so keep
                    # the node itself.
                    node_data = {
                        'type': 'emitter',
                        'node_name': node_name,
                        'node_number': node_number,
                        'matrix': controllers.global_transform.copy(),
                    }

                if node_data and node_type not in (NODE_TYPE_SPEEDTREE, NODE_TYPE_LIGHT,
                                                   NODE_TYPE_EMITTER):
                    node_data['node_name'] = node_name
                    node_data['node_offset'] = node_pos
                    node_data['node_number'] = node_number
            
            child_data_list = []
            for child_offset in children:
                seek_pos = self.model_data.offset_model_data + child_offset
                self.reader.seek(seek_pos)
                child_data = self.load_node(controllers.global_transform, child_offset, bone)
                if child_data:
                    child_data_list.extend(child_data)
            
            result = []
            if node_data:
                result.append(node_data)
            result.extend(child_data_list)
            
            logger.pop()
            return result
        
        except Exception as e:
            logger.error(f"Error loading node at 0x{node_pos:X}: {e}")
            traceback.print_exc()
            return []
    
    def process_speedtree_files(self):
        """Convert .spt files to .fbx using Spt2Fbx.exe"""
        if not self.import_speedtrees or not self.speedtree_types:
            return False
        
        trees_source_dir = None
        for folder in ["trees00", "trees"]:
            potential_dir = os.path.join(self.game_root, folder)
            if os.path.exists(potential_dir):
                trees_source_dir = potential_dir
                break
        
        if not trees_source_dir:
            logger.log(f"Trees folder not found in {self.game_root}. Skipping SpeedTree processing.")
            return False
        
        spt2fbx_exe_path = os.path.join(os.path.dirname(bpy.app.binary_path), "Spt2Fbx.exe")
        
        if not os.path.exists(spt2fbx_exe_path):
            logger.log(f"Spt2Fbx.exe not found at {spt2fbx_exe_path}. Cannot process SpeedTrees.")
            return False
        
        trees_needing_conversion = set()
        trees_with_fbx = set()
        
        for tree_type in self.speedtree_types:
            possible_tree_names = [tree_type]
            if not re.search(r'_\d+$', tree_type):
                for suffix in ['_01', '_1', '_001']:
                    possible_tree_names.append(tree_type + suffix)
            
            base_without_number = re.sub(r'_\d+$', '', tree_type)
            if base_without_number != tree_type:
                possible_tree_names.append(base_without_number + '_01')
            
            has_fbx = False
            has_spt = False
            spt_path = None
            
            for test_name in possible_tree_names:
                for ext in ['.fbx', '.FBX']:
                    test_fbx = os.path.join(trees_source_dir, test_name + ext)
                    if os.path.exists(test_fbx):
                        has_fbx = True
                        logger.log(f"  Found existing FBX for {tree_type}: {test_name}.fbx")
                        break
                
                for ext in ['.spt', '.SPT']:
                    test_spt = os.path.join(trees_source_dir, test_name + ext)
                    if os.path.exists(test_spt):
                        has_spt = True
                        spt_path = test_spt
                        logger.log(f"  Found SPT for {tree_type}: {test_name}.spt")
                        break
                
                if has_fbx:
                    break
            
            if has_fbx:
                trees_with_fbx.add(tree_type)
            elif has_spt:
                trees_needing_conversion.add(tree_type)
                logger.log(f"  {tree_type} needs conversion (SPT found, no FBX)")
            else:
                logger.log(f"  {tree_type}: Neither SPT nor FBX found")
        
        # Run Spt2Fbx.exe only if there are trees needing conversion
        if trees_needing_conversion:
            logger.log(f"\nRunning Spt2Fbx.exe to convert {len(trees_needing_conversion)} tree types...")
            try:
                command = [spt2fbx_exe_path, trees_source_dir]
                logger.log(f"Running: {' '.join(command)}")
                
                result = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                )
                
                logger.log("Spt2Fbx.exe completed successfully.")
                
                converted_count = 0
                for tree_type in trees_needing_conversion:
                    possible_tree_names = [tree_type]
                    if not re.search(r'_\d+$', tree_type):
                        for suffix in ['_01', '_1', '_001']:
                            possible_tree_names.append(tree_type + suffix)
                    
                    base_without_number = re.sub(r'_\d+$', '', tree_type)
                    if base_without_number != tree_type:
                        possible_tree_names.append(base_without_number + '_01')
                    
                    for test_name in possible_tree_names:
                        test_fbx = os.path.join(trees_source_dir, test_name + '.fbx')
                        if os.path.exists(test_fbx):
                            converted_count += 1
                            logger.log(f"    Successfully converted: {test_name}.fbx")
                            break
                
                logger.log(f"Successfully converted {converted_count}/{len(trees_needing_conversion)} tree types")
                return converted_count > 0
                
            except subprocess.CalledProcessError as e:
                logger.log(f"Error running Spt2Fbx.exe: Exit code {e.returncode}")
                if e.stdout:
                    logger.log(f"stdout: {e.stdout}")
                if e.stderr:
                    logger.log(f"stderr: {e.stderr}")
                return False
            except Exception as e:
                logger.log(f"Unexpected error during SpeedTree processing: {e}")
                return False
        else:
            if trees_with_fbx:
                logger.log(f"\nAll required trees already have FBX files. Skipping conversion.")
            else:
                logger.log(f"\nNo SPT files found for required trees. Skipping conversion.")
            return True
    
    def extract_texture_names_from_spt(self, spt_path):
        """Extract texture names from .spt file"""
        if not os.path.exists(spt_path):
            return None, None
        
        try:
            with open(spt_path, 'rb') as f:
                content = f.read()
            
            try:
                text = content.decode('latin-1')
            except:
                text = content.decode('utf-8', errors='ignore')
            
            cm_texture = None
            
            cm_prefix_matches = re.findall(r'\b(cm_[a-zA-Z0-9_]+?)(?:#|\.|\s|$)', text, re.IGNORECASE)
            if cm_prefix_matches:
                valid_matches = []
                for match in cm_prefix_matches:
                    clean_match = re.sub(r'[^\x20-\x7E]', '', match)
                    if len(clean_match) >= 3 and len(clean_match) <= 30:
                        valid_matches.append(clean_match)
                
                if valid_matches:
                    valid_matches.sort(key=len)
                    cm_texture = valid_matches[0]
                    logger.log(f"    Found cm_ prefixed texture: {cm_texture}")
            
            if not cm_texture:
                cm_suffix_matches = re.findall(r'\b([a-zA-Z][a-zA-Z0-9_]*_[cC][mM])\b', text)
                if cm_suffix_matches:
                    valid_matches = []
                    for match in cm_suffix_matches:
                        clean_match = re.sub(r'[^\x20-\x7E]', '', match)
                        if (len(clean_match) >= 5 and len(clean_match) <= 30 and 
                            '_' in clean_match and 
                            not clean_match.startswith('__')):
                            valid_matches.append(clean_match)
                    
                    if valid_matches:
                        valid_matches.sort(key=len)
                        cm_texture = valid_matches[0]
                        logger.log(f"    Found frond/leaf texture reference: {cm_texture}")
            
            branch_texture = None
            tga_matches = re.findall(r'([a-zA-Z0-9_]+\.tga)', text, re.IGNORECASE)
            
            if tga_matches:
                for match in tga_matches:
                    clean_match = re.sub(r'[^\x20-\x7E]', '', match)
                    clean_match = clean_match.replace('.tga', '').replace('.TGA', '')
                    if len(clean_match) >= 3 and len(clean_match) <= 30:
                        branch_texture = clean_match
                        logger.log(f"    Found branch texture reference: {match} -> {clean_match}")
                        break
            
            if not branch_texture:
                kora_matches = re.findall(r'([a-zA-Z0-9_]*kora[a-zA-Z0-9_]*)', text, re.IGNORECASE)
                if kora_matches:
                    branch_texture = kora_matches[0]
                    logger.log(f"    Found potential bark texture: {branch_texture}")
            
            return cm_texture, branch_texture
            
        except Exception as e:
            logger.log(f"    Error reading .spt file: {e}")
            return None, None
    
    def load_tree_fbx(self, tree_name, trees_dir):
        """Load an FBX file for a specific tree and return imported objects"""
        
        possible_tree_names = [tree_name]
        if not re.search(r'_\d+$', tree_name):
            for suffix in ['_01', '_1', '_001']:
                possible_tree_names.append(tree_name + suffix)
        
        base_without_number = re.sub(r'_\d+$', '', tree_name)
        if base_without_number != tree_name:
            possible_tree_names.append(base_without_number + '_01')
        
        fbx_path = None
        spt_path = None
        actual_loaded_name = None
        
        for test_name in possible_tree_names:
            for ext in ['.fbx', '.FBX']:
                test_fbx = os.path.join(trees_dir, test_name + ext)
                if os.path.exists(test_fbx):
                    fbx_path = test_fbx
                    actual_loaded_name = test_name
                    break
            
            for ext in ['.spt', '.SPT']:
                test_spt = os.path.join(trees_dir, test_name + ext)
                if os.path.exists(test_spt):
                    spt_path = test_spt
                    actual_loaded_name = test_name
                    break
            
            if fbx_path or spt_path:
                break
        
        if actual_loaded_name:
            logger.log(f"  Found matching files for: {actual_loaded_name} (from requested: {tree_name})")
        else:
            logger.log(f"  No files found for tree: {tree_name}")
            return None
        
        cm_texture_name = None
        branch_texture_name = None
        cm_texture_path = None
        branch_texture_path = None
        
        if spt_path:
            logger.log(f"  Found .spt file: {spt_path}")
            cm_texture_name, branch_texture_name = self.extract_texture_names_from_spt(spt_path)
            
            if cm_texture_name:
                cm_texture_path = self.find_texture_file(cm_texture_name)
                if cm_texture_path:
                    logger.log(f"    Found frond/leaf texture: {cm_texture_path}")
            
            if branch_texture_name:
                branch_texture_path = self.find_texture_file(branch_texture_name)
                if branch_texture_path:
                    logger.log(f"    Found branch texture: {branch_texture_path}")
        
        if not fbx_path:
            logger.log(f"  No FBX found for tree: {tree_name}")
            return None
        
        try:
            old_selection = set(bpy.context.selected_objects)
            old_active = bpy.context.view_layer.objects.active
            
            bpy.ops.import_scene.fbx(
                filepath=fbx_path,
                global_scale=TREE_SCALE_MULTIPLIER,
                use_image_search=False,
                use_alpha_decals=False,
                decal_offset=0.0,
                use_anim=False,
                use_custom_normals=True
            )
            
            new_objects = [obj for obj in bpy.context.selected_objects if obj not in old_selection]
            
            imported_objects = []
            
            if new_objects:
                processed_materials = set()
                
                for obj in new_objects:
                    if obj.type == 'MESH':
                        if obj.data.materials:
                            for mat in obj.data.materials:
                                if mat and mat.name not in processed_materials:
                                    if "FrondMAT" in mat.name or "LeafMAT" in mat.name:
                                        if cm_texture_path:
                                            self.apply_texture_to_material(mat, cm_texture_path, is_branch=False)
                                    elif "BranchMAT" in mat.name:
                                        if branch_texture_path:
                                            self.apply_texture_to_material(mat, branch_texture_path, is_branch=True)
                                    processed_materials.add(mat.name)
                        
                        imported_objects.append({
                            'obj': obj,
                            'location': obj.location.copy(),
                            'rotation': obj.rotation_euler.copy(),
                            'scale': obj.scale.copy(),
                            'mesh': obj.data,
                            'materials': [mat for mat in obj.data.materials]
                        })
                
                for obj in new_objects:
                    for collection in obj.users_collection:
                        collection.objects.unlink(obj)
                
                bpy.ops.object.select_all(action='DESELECT')
                
                logger.log(f"    Stored {len(imported_objects)} objects for {tree_name}")
                return imported_objects
            else:
                return None
                
        except Exception as e:
            logger.log(f"  Error loading FBX {fbx_path}: {e}")
            return None
    
    def apply_texture_to_material(self, material, texture_path, is_branch=False):
        """Apply a texture to a material's base color and alpha"""
        if not material or not texture_path:
            return False

        material.specular_intensity = 0.0
        
        nodes = material.node_tree.nodes
        links = material.node_tree.links
        
        bsdf = None
        for node in nodes:
            if node.type == 'BSDF_PRINCIPLED':
                bsdf = node
                break
        
        if not bsdf:
            bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        
        bsdf.inputs['Specular IOR Level'].default_value = 0.0
        
        try:
            img = bpy.data.images.load(texture_path)
            
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.image = img
            tex_node.location = (-300, 300)
            tex_node.label = os.path.basename(texture_path)
            
            links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
            
            if not is_branch:
                links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
            
            return True
            
        except Exception as e:
            logger.log(f"    Failed to load texture {texture_path}: {e}")
            return False

    
    def create_tree_instances(self, instances_collection):
        """Create all tree instances as linked duplicates"""
        if not self.speedtree_instances:
            return 0
        
        logger.log(f"\nCreating {len(self.speedtree_instances)} instances")
        
        instance_count = 0
        missing_types = set()
        
        instances_by_type = defaultdict(list)
        for inst in self.speedtree_instances:
            instances_by_type[inst.tree_type].append(inst)
        
        trees_dir = None
        for folder in ["trees00", "trees"]:
            potential_dir = os.path.join(self.game_root, folder)
            if os.path.exists(potential_dir):
                trees_dir = potential_dir
                break
        
        if not trees_dir:
            logger.log("Cannot find trees directory. Aborting.")
            return 0
        
        logger.log(f"Loading {len(self.speedtree_types)} required tree types")
        tree_templates = {}
        
        all_master_objects = []
        
        for tree_type in self.speedtree_types:
            logger.log(f"\nLoading tree type: {tree_type}")
            templates = self.load_tree_fbx(tree_type, trees_dir)
            if templates:
                tree_templates[tree_type] = templates
        
        # For each tree type, create master meshes (temporary)
        for tree_type, templates in tree_templates.items():
            master_objects = []
            for template_idx, template_data in enumerate(templates):
                imported_obj = template_data['obj']
                
                master_mesh = imported_obj.data.copy()
                master_mesh.name = f"{tree_type}_master_{template_idx:02d}"
                
                master_obj = bpy.data.objects.new(
                    f"TEMP_{tree_type}_master_{template_idx:02d}", 
                    master_mesh
                )
                
                # Apply template's local transform to the master object
                master_obj.location = template_data['location'].copy()
                master_obj.rotation_euler = template_data['rotation'].copy()
                master_obj.scale = template_data['scale'].copy()

                for mat in template_data['materials']:
                    if mat not in master_obj.data.materials.values():
                        master_obj.data.materials.append(mat)

                instances_collection.objects.link(master_obj)
                master_objects.append(master_obj)
                all_master_objects.append(master_obj)
                
                logger.log(f"    Created temporary master mesh: {master_obj.name}")
            
            tree_templates[tree_type] = master_objects
        
        # Create instances using linked duplicates
        for tree_type, type_instances in instances_by_type.items():
            if tree_type in tree_templates:
                master_objects = tree_templates[tree_type]
                logger.log(f"  Creating {len(type_instances)} instances of {tree_type}")
                
                for inst_idx, inst in enumerate(type_instances):
                    for master_obj in master_objects:
                        new_obj = bpy.data.objects.new(
                            f"{tree_type}_{instance_count:06d}", 
                            master_obj.data
                        )
                        
                        new_obj.location = master_obj.location.copy()
                        new_obj.rotation_euler = master_obj.rotation_euler.copy()
                        new_obj.scale = master_obj.scale.copy()
                        new_obj.location += inst.position
                        
                        # Hack - scale by -1 on X to get the correct tree orientation
                        new_obj.scale.x *= -1
                        
                        instances_collection.objects.link(new_obj)
                    
                    instance_count += 1
            else:
                missing_types.add(tree_type)
        
        logger.log(f"\nCleaning up {len(all_master_objects)} temporary master objects...")
        bpy.ops.object.select_all(action='DESELECT')
        
        for master_obj in all_master_objects:
            if master_obj.name in bpy.data.objects:
                master_obj.select_set(True)
        
        if all_master_objects:
            bpy.ops.object.delete()
        
        for obj in bpy.data.objects:
            if obj.name.startswith("TEMP_") or (obj.name.startswith("Cube") and obj.name not in bpy.context.scene.collection.all_objects):
                bpy.data.objects.remove(obj, do_unlink=True)
        
        if missing_types:
            logger.log(f"  Warning: Missing base models for types: {', '.join(missing_types)}")
        
        return instance_count
    
    def import_model(self):
        logger.log(f"\n{'='*60}", force=True)
        logger.log(f"Importing: {self.filepath}", force=True)
        logger.log(f"{'='*60}\n", force=True)
        
        try:
            is_binary = self.reader.read_u8()
            if is_binary != 0:
                self.reader.seek(0)
                type_str = self.reader.read_string_until_null()
                if type_str.startswith("binarycompositemodel"):
                    logger.error("binarycompositemodel not supported")
                    return None
                logger.error("Not a Witcher MDB file")
                return None
            
            self.reader.seek(4)
            
            version = self.reader.read_u32()
            self.model_data.file_version = version & 0x0FFFFFFF
            logger.log(f"File version: {self.model_data.file_version}", force=True)
            
            if self.model_data.file_version not in [FILE_VERSION_133, FILE_VERSION_136]:
                logger.error(f"Unsupported version: {self.model_data.file_version}")
                return None
            
            model_count = self.reader.read_u32()
            logger.log(f"Model count: {model_count}", force=True)
            
            if model_count != 1:
                logger.error(f"Unsupported model count: {model_count}")
                return None
            
            self.reader.seek(4, 1)
            
            self.model_data.size_model_data = self.reader.read_u32()
            self.reader.seek(4, 1)
            self.model_data.offset_model_data = 32
            
            if self.model_data.file_version == FILE_VERSION_133:
                self.model_data.offset_raw_data = self.reader.read_u32() + self.model_data.offset_model_data
                self.model_data.size_raw_data = self.reader.read_u32()
                self.model_data.offset_tex_data = self.model_data.offset_model_data
                self.model_data.size_tex_data = 0
            else:
                self.model_data.offset_raw_data = self.model_data.offset_model_data
                self.model_data.size_raw_data = 0
                self.model_data.offset_tex_data = self.reader.read_u32() + self.model_data.offset_model_data
                self.model_data.size_tex_data = self.reader.read_u32()
            
            self.reader.seek(8, 1)
            
            self.model_name = self.reader.read_string(64)
            logger.log(f"Model name: {self.model_name}", force=True)
            
            offset_root_node = self.reader.read_u32()
            logger.log(f"Root node offset: 0x{offset_root_node:X}", force=True)
            
            self.reader.seek(32, 1)
            
            type_byte = self.reader.read_u8()
            
            self.reader.seek(3, 1)
            self.reader.seek(48, 1)
            
            first_lod = self.reader.read_f32()
            last_lod = self.reader.read_f32()
            logger.log(f"LOD range: {first_lod} - {last_lod}", force=True)
            
            self.reader.seek(16, 1)
            
            detail_map = self.reader.read_string(64)
            self.reader.seek(4, 1)
            
            self.model_scale = self.reader.read_f32()
            logger.log(f"Model scale: {self.model_scale}", force=True)
            
            self.super_model = self.reader.read_string(64)
            logger.log(f"Super model: {self.super_model}", force=True)
            
            self.reader.seek(4 + 16, 1)
            
            root_pos = self.model_data.offset_model_data + offset_root_node
            self.reader.seek(root_pos)
            mesh_list = self.load_node()
            
            if self.import_speedtrees and self.speedtree_types:
                self.process_speedtree_files()
            
            logger.log(f"\nLoaded {len(mesh_list)} meshes", force=True)
            if self.import_speedtrees:
                logger.log(f"Found {len(self.speedtree_instances)} SpeedTree instances", force=True)
            
            return mesh_list
        
        except Exception as e:
            logger.error(f"Import failed: {e}")
            traceback.print_exc()
            return None
    
    def close(self):
        self.reader.close()

# ============
# BLENDER OPERATOR
# ============

class IMPORT_MDB_OT_operator(Operator, ImportHelper):
    """Import Witcher 1 MDB model file"""
    
    bl_idname = "import_scene.mdb"
    bl_label = "Import Witcher MDB"
    bl_options = {'REGISTER', 'UNDO'}
    
    filename_ext = ".mdb"
    
    filter_glob: StringProperty(
        default="*.mdb",
        options={'HIDDEN'},
    )
    
    game_path: StringProperty(
        name="Game Root Path",
        description="(OPTIONAL) Path to The Witcher 1 unpacked files",
        default="",
        subtype='DIR_PATH',
    )
    
    time_of_day: EnumProperty(
        name="Time of Day",
        description="Select time of day for lightmaps (None = no lightmaps)",
        items=[
            ('DAY', "Day", "Use day lightmaps (!d suffix)"),
            ('MORNING', "Morning", "Use morning lightmaps (!r suffix)"),
            ('NOON', "Noon", "Use noon lightmaps (!p suffix)"),
            ('EVENING', "Evening", "Use evening lightmaps (!w suffix)"),
            ('NIGHT', "Night", "Use night lightmaps (!n suffix)"),
            ('NONE', "None", "Don't load lightmaps"),
        ],
        default='DAY',
    )
    import_speedtrees=False,
    import_speedtrees: BoolProperty(
        name="Import SpeedTrees",
        description="Import SpeedTree instances (requires Spt2Fbx.exe in Blender directory)",
        default=True,
    )

    import_skeletons: BoolProperty(
        name="Import Skeletons",
        description="Import skeleton and skinning data (for animated models)",
        default=False,
    )

    debug_mode: BoolProperty(
        name="Debug Mode",
        description="Enable detailed logging",
        default=False,
    )
    
    def execute(self, context):
        global logger
        logger.enabled = self.debug_mode
        # Materials are shared across this import and no further; see
        # _material_built_here.
        _import_materials.clear()
        
        logger.log(f"\n{'='*60}", force=True)
        logger.log(f"Blender Witcher MDB Importer", force=True)
        logger.log(f"{'='*60}", force=True)
        
        importer = None

        try:
            model_path = self.filepath
            composite = read_composite_model(self.filepath)
            if composite:
                resolved = find_sibling_model(self.filepath, composite.base_model)
                if resolved is None:
                    self.report({'ERROR'},
                                f"'{os.path.basename(self.filepath)}' is a composite model "
                                f"naming '{composite.base_model}', which is not beside it")
                    return {'CANCELLED'}
                logger.log(f"Composite model -> {composite.base_model} "
                           f"({len(composite.animation_sets)} animation sets)", force=True)
                model_path = resolved

            importer = MDBImporter(
                model_path,
                self.game_path,
                self.time_of_day,
                self.import_speedtrees,
                self.import_skeletons,
                self.debug_mode
            )
            node_data_list = importer.import_model()
            if node_data_list is None:
                self.report({'ERROR'}, "Not a Witcher MDB file")
                return {'CANCELLED'}

            has_trees = self.import_speedtrees and importer.speedtree_instances
            if not node_data_list and not has_trees and not importer.bone_list:
                self.report({'ERROR'}, "No meshes, lights, emitters or bones found in file")
                return {'CANCELLED'}

            base_name = os.path.splitext(os.path.basename(self.filepath))[0]
            collection = bpy.data.collections.new(base_name)
            context.scene.collection.children.link(collection)
            
            layer_collection = context.view_layer.layer_collection
            target_layer = None
            for lc in layer_collection.children:
                if lc.name == base_name:
                    target_layer = lc
                    break
            
            if target_layer:
                context.view_layer.active_layer_collection = target_layer
            
            imported_count = 0
            light_count = 0
            tree_instance_count = 0
            emitter_count = 0
            
            reflection_probes = []
            armature_obj = None
            if importer.bone_list:
                logger.log(f"Creating armature with {len(importer.bone_list)} bones", force=True)
                armature_obj = self._create_armature(base_name, importer.bone_list, importer.root_bones)
            
            if node_data_list:
                mesh_data_list = [data for data in node_data_list if 'vertices' in data or 'layers' in data]
                light_data_list = [data for data in node_data_list if data.get('type') == 'light']
                
                # Create meshes
                for idx, mesh_data in enumerate(mesh_data_list):
                    logger.log(f"Creating mesh {idx + 1}/{len(mesh_data_list)}", force=True)
                    
                    if len(mesh_data['vertices']) < 3 or len(mesh_data['indices']) < 3:
                        logger.log("  Skipping: insufficient geometry")
                        continue
                    
                    obj = self._create_mesh_object(
                        f"{base_name}",
                        mesh_data,
                        importer,
                        armature_obj
                    )
                    if obj:
                        collection.objects.link(obj)
                        imported_count += 1
                        if is_mirror_surface(mesh_data):
                            probe = create_reflection_probe(obj, mesh_data)
                            if probe:
                                collection.objects.link(probe)
                                reflection_probes.append(probe)
                
                # Create lights
                for light_data in light_data_list:
                    light_obj = importer.create_light_object(light_data, collection)
                    if light_obj:
                        light_count += 1

                # Create emitters as empties - the effect itself cannot be
                # rebuilt, but its position and name are worth keeping.
                for emitter_data in [d for d in node_data_list if d.get('type') == 'emitter']:
                    empty = bpy.data.objects.new(emitter_data['node_name'], None)
                    empty.empty_display_type = 'SPHERE'
                    empty.empty_display_size = 0.1
                    empty.matrix_world = emitter_data['matrix']
                    empty["tw1_node_type"] = "emitter"
                    collection.objects.link(empty)
                    if armature_obj and emitter_data['node_name'] in armature_obj.data.bones:
                        empty.parent = armature_obj
                        empty.parent_type = 'BONE'
                        empty.parent_bone = emitter_data['node_name']
                    emitter_count += 1
            
            if self.import_speedtrees and importer.speedtree_instances:
                instances_collection_name = f"ST_Instances_{base_name}"
                if instances_collection_name not in bpy.data.collections:
                    instances_collection = bpy.data.collections.new(instances_collection_name)
                    context.scene.collection.children.link(instances_collection)
                else:
                    instances_collection = bpy.data.collections[instances_collection_name]
                
                tree_instance_count = importer.create_tree_instances(instances_collection)
            
            if target_layer:
                context.view_layer.active_layer_collection = layer_collection
            
            logger.log(f"Imported {imported_count} meshes, {light_count} lights", force=True)
            if tree_instance_count:
                logger.log(f"Created {tree_instance_count} SpeedTree instances", force=True)
            if armature_obj:
                logger.log(f"Created armature with {len(importer.bone_list)} bones", force=True)

            if composite:
                # Keep the pack list where the user (and the .mba importer) can
                # find it; a composite model is mostly a pointer to these.
                holder = armature_obj if armature_obj else collection
                holder["tw1_base_model"] = composite.base_model
                holder["tw1_animation_sets"] = composite.animation_sets
                logger.log(f"Animation sets: {', '.join(composite.animation_sets)}", force=True)

            if reflection_probes:
                # A planar probe is dead weight in EEVEE unless raytracing is on.
                eevee = getattr(context.scene, 'eevee', None)
                if (eevee is not None and hasattr(eevee, 'use_raytracing')
                        and not eevee.use_raytracing):
                    eevee.use_raytracing = True
                    logger.log("Enabled EEVEE raytracing for the reflection probes",
                               force=True)

            if importer.unresolved_textures:
                # A mesh with no texture at all is the white one someone will
                # ask about later, so say which name went looking for nothing.
                names = ", ".join(sorted(importer.unresolved_textures))
                logger.log(f"Textures named by this model but not in the game's "
                           f"files: {names}", force=True)

            summary = (f"Imported {imported_count} meshes, {light_count} lights, "
                       f"{emitter_count} emitters, {tree_instance_count} trees, "
                       f"{len(importer.bone_list)} bones")
            if reflection_probes:
                summary += f", {len(reflection_probes)} reflection planes"
            if composite:
                summary += (f" (composite of {composite.base_model}, "
                            f"{len(composite.animation_sets)} animation sets)")
            self.report({'INFO'}, summary)
            return {'FINISHED'}
        
        except Exception as e:
            logger.error(f"Import failed: {e}")
            traceback.print_exc()
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        
        finally:
            if importer:
                importer.close()

    def _create_water_material(self, mesh_data, importer, mat_name=None):
        """Create a material for water shaders"""
        if not mat_name:
            mat_name = "WaterMaterial"
        
        mat = bpy.data.materials.new(name=mat_name)
        mat.specular_intensity = 0.0
        apply_backface_culling(mat, mesh_data)
        mat.node_tree.nodes.clear()
        
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (800, 0)
        
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (600, 0)
        bsdf.inputs['Roughness'].default_value = 0.0
        bsdf.inputs['IOR'].default_value = 1.33
        bsdf.inputs['Specular IOR Level'].default_value = 0.5
        bsdf.inputs['Alpha'].default_value = 0.04
        
        water_params = mesh_data.get('water_params', {})
        if water_params and 'water_color' in water_params:
            color = water_params['water_color']
            bsdf.inputs['Base Color'].default_value = color
            logger.log(f"  Set water color from params: {color}")
        else:
            bsdf.inputs['Base Color'].default_value = (0.25, 0.37, 0.3, 1.0)
        
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        mat.blend_method = 'BLEND'
        
        uv_map1 = nodes.new('ShaderNodeUVMap')
        uv_map1.location = (-400, -200)
        uv_map1.uv_map = "UVMap.1"
        uv_map1.label = "UVMap.1 (Bump)"
        
        bump_texture = None
        if water_params and 'bump_texture' in water_params:
            bump_tex_name = water_params['bump_texture']
            bump_tex_path = importer.find_texture_file(bump_tex_name)
            if bump_tex_path:
                bump_tex_node = nodes.new('ShaderNodeTexImage')
                bump_tex_node.location = (-200, -200)
                bump_tex_node.label = f"Bump: {os.path.basename(bump_tex_name)}"
                
                try:
                    img = bpy.data.images.load(bump_tex_path)
                    img.colorspace_settings.name = 'Non-Color'
                    bump_tex_node.image = img
                    links.new(uv_map1.outputs['UV'], bump_tex_node.inputs['Vector'])
                    
                    normal_map = nodes.new('ShaderNodeNormalMap')
                    normal_map.location = (0, -200)
                    normal_map.space = 'TANGENT'
                    
                    links.new(bump_tex_node.outputs['Color'], normal_map.inputs['Color'])
                    links.new(normal_map.outputs['Normal'], bsdf.inputs['Normal'])
                    logger.log(f"  Connected bump texture to normal: {bump_tex_path}")
                except Exception as e:
                    logger.log(f"  Failed to load bump texture {bump_tex_name}: {e}")
        
        return mat

    def _create_armature(self, name_prefix, bones, root_bones):
        """Create an armature with bones from the imported data"""
        if not bones:
            return None
        
        armature_data = bpy.data.armatures.new(name_prefix + "_ARM")
        armature_obj = bpy.data.objects.new(name_prefix + "_Armature", armature_data)
        armature_obj.show_in_front = True
        
        collection = bpy.context.collection
        collection.objects.link(armature_obj)
        
        bpy.context.view_layer.objects.active = armature_obj
        bpy.ops.object.mode_set(mode='EDIT')
        
        edit_bones = armature_data.edit_bones
        
        processed_bones = {}
        
        def process_bone_hierarchy(bone_data):
            if bone_data.name in processed_bones:
                return
            
            eb = edit_bones.new(bone_data.name)
            
            bone_pos = bone_data.global_matrix.to_translation()
            
            eb.head = bone_pos
            
            if bone_data.children:
                first_child = bone_data.children[0]
                child_pos = first_child.global_matrix.to_translation()
                direction = child_pos - bone_pos
                if direction.length > 0.001:
                    eb.tail = bone_pos + direction.normalized() * min(direction.length * 0.2, 0.5)
                else:
                    eb.tail = bone_pos + Vector((0, 0.1, 0))
            else:
                if bone_data.parent:
                    parent_pos = bone_data.parent.global_matrix.to_translation()
                    direction = bone_pos - parent_pos
                    if direction.length > 0.001:
                        eb.tail = bone_pos + direction.normalized() * 0.2
                    else:
                        eb.tail = bone_pos + Vector((0, 0.1, 0))
                else:
                    eb.tail = bone_pos + Vector((0, 0.2, 0))
            
            processed_bones[bone_data.name] = eb
            
            for child in bone_data.children:
                process_bone_hierarchy(child)
        
        for root_bone in root_bones:
            process_bone_hierarchy(root_bone)
        
        for bone_data in bones:
            if bone_data.parent and bone_data.name in processed_bones and bone_data.parent.name in processed_bones:
                eb = processed_bones[bone_data.name]
                parent_eb = processed_bones[bone_data.parent.name]
                eb.parent = parent_eb
        
        bpy.ops.object.mode_set(mode='OBJECT')

        # Edit bones are shaped for readability, so their rest matrices do not match the
        # node frames animations are authored against. Keep those for the .mba importer.
        for bone_data in bones:
            bone = armature_data.bones.get(bone_data.name)
            if bone is None:
                continue
            bone[REST_LOCAL_PROP] = matrix_to_list(bone_data.local_matrix)
            bone[REST_GLOBAL_PROP] = matrix_to_list(bone_data.global_matrix)

        armature_obj.hide_viewport = False
        armature_obj.hide_render = False

        return armature_obj

    def _create_mesh_object(self, name_prefix, mesh_data, importer, armature_obj=None):
        """Create a Blender mesh object with proper UV sets and skinning"""
        vertices = mesh_data['vertices']
        indices = mesh_data['indices']
        
        if len(vertices) < 3 or len(indices) < 3:
            return None
        
        try:
            node_name = mesh_data.get('node_name', 'unknown')
            node_offset = mesh_data.get('node_offset', 0)
            
            obj_name = f"{node_name}_0x{node_offset:X}"
            
            logger.log(f"  Creating mesh object: {obj_name}")
            
            mesh = bpy.data.meshes.new(obj_name)
            
            polygons = []
            for i in range(0, len(indices), 3):
                if i + 2 < len(indices):
                    polygons.append((indices[i], indices[i+1], indices[i+2]))
            
            mesh.from_pydata(vertices, [], polygons)
            mesh.update()
            
            # Create object
            obj = bpy.data.objects.new(obj_name, mesh)
            
            if 'skin_weights' in mesh_data and mesh_data['skin_weights'] and armature_obj:
                logger.log(f"  Applying skinning to {obj_name}")
                
                if mesh_data.get('bone_node') and mesh_data['bone_node'].parent:
                    obj.location = (0, 0, 0)
                    obj.rotation_euler = (0, 0, 0)
                    obj.scale = (1, 1, 1)
                
                obj.parent = armature_obj

                modifier = obj.modifiers.new(name="Armature", type='ARMATURE')
                modifier.object = armature_obj
                modifier.use_vertex_groups = True
                modifier.use_bone_envelopes = False
                
                vertex_groups = {}
                bone_names = set()
                
                # First pass: collect all bone names that have weights
                for v_idx, weights in enumerate(mesh_data['skin_weights']):
                    for bone_name, weight in weights:
                        bone_names.add(bone_name)
                
                for bone_name in bone_names:
                    if bone_name not in vertex_groups:
                        vg = obj.vertex_groups.new(name=bone_name)
                        vertex_groups[bone_name] = vg
                        logger.log(f"    Created vertex group: {bone_name}")
                
                # Second pass: assign weights
                for v_idx, weights in enumerate(mesh_data['skin_weights']):
                    for bone_name, weight in weights:
                        if bone_name in vertex_groups:
                            vertex_groups[bone_name].add([v_idx], weight, 'REPLACE')
                
                logger.log(f"    Created {len(vertex_groups)} vertex groups with skinning weights")
                
                # Verify that some vertices have weights
                weighted_verts = sum(1 for w in mesh_data['skin_weights'] if w)
                logger.log(f"    {weighted_verts}/{len(mesh_data['skin_weights'])} vertices have weights")

            elif armature_obj and node_name in armature_obj.data.bones:
                # Rigid parts (eyes, teeth, weapons) are trimesh nodes rather than skins; bind
                # them fully to the node they hang off so they follow the animation.
                obj.parent = armature_obj

                modifier = obj.modifiers.new(name="Armature", type='ARMATURE')
                modifier.object = armature_obj
                modifier.use_vertex_groups = True
                modifier.use_bone_envelopes = False

                vg = obj.vertex_groups.new(name=node_name)
                vg.add(list(range(len(vertices))), 1.0, 'REPLACE')
                logger.log(f"    Rigidly bound {obj_name} to bone '{node_name}'")

            # Add UV layers
            if mesh_data.get('is_texture_paint') and mesh_data.get('layers'):
                if mesh_data.get('base_uvs') and len(mesh_data['base_uvs']) == len(vertices):
                    uv_layer = mesh.uv_layers.new(name="UVMap")
                    if uv_layer:
                        for i, loop in enumerate(mesh.loops):
                            if loop.vertex_index < len(mesh_data['base_uvs']):
                                uv_layer.data[i].uv = mesh_data['base_uvs'][loop.vertex_index]
                
                if mesh_data.get('lightmap_uvs') and len(mesh_data['lightmap_uvs']) == len(vertices):
                    uv_layer2 = mesh.uv_layers.new(name="LightmapUV")
                    if uv_layer2:
                        for i, loop in enumerate(mesh.loops):
                            if loop.vertex_index < len(mesh_data['lightmap_uvs']):
                                uv_layer2.data[i].uv = mesh_data['lightmap_uvs'][loop.vertex_index]
                
                valid_layers = texture_paint_layers(mesh_data, importer)
                
                if valid_layers:
                    layer_weights = [list(layer['weights']) for _, layer in valid_layers]
                    filled = fill_unpainted_weights(mesh, layer_weights)
                    if filled:
                        logger.log(f"    Filled {filled} vertices left unpainted by a "
                                   f"disabled layer")

                    for batch_idx in range(0, len(valid_layers), 3):
                        batch_layers = valid_layers[batch_idx:batch_idx + 3]

                        # FLOAT_COLOR, not a byte colour layer: byte colours are
                        # treated as sRGB and would be gamma-converted on the way
                        # into the shader, which silently skews every weight.
                        name = f"Weights_{batch_idx // 3}"
                        vcol_layer = mesh.color_attributes.new(
                            name=name, type='FLOAT_COLOR', domain='CORNER')

                        if vcol_layer:
                            for loop in mesh.loops:
                                vert_idx = loop.vertex_index
                                rgb = [0.0, 0.0, 0.0, 1.0]

                                for channel_idx in range(len(batch_layers)):
                                    weights = layer_weights[batch_idx + channel_idx]
                                    if channel_idx < 3 and vert_idx < len(weights):
                                        rgb[channel_idx] = weights[vert_idx]

                                vcol_layer.data[loop.index].color = tuple(rgb)

                            logger.log(f"    Created weight layer {name} with {len(batch_layers)} layers")
            
            elif 'uv_sets' in mesh_data and mesh_data['uv_sets']:
                for uv_idx, uv_set in enumerate(mesh_data['uv_sets']):
                    if uv_idx >= 4:
                        break

                    # An empty slot makes no layer, but it still takes its
                    # number: the layer names are what the materials look the
                    # slots up by.
                    if uv_set and len(uv_set) == len(vertices):
                        uv_name = "UVMap" if uv_idx == 0 else f"UVMap.{uv_idx}"
                        
                        uv_layer = mesh.uv_layers.new(name=uv_name)
                        if uv_layer:
                            for i, loop in enumerate(mesh.loops):
                                if loop.vertex_index < len(uv_set):
                                    uv_layer.data[i].uv = uv_set[loop.vertex_index]
            
            # Shade smooth first: a sharp (flat) face overrides the custom split
            # normals on its corners, so this has to happen before they are set.
            for poly in mesh.polygons:
                poly.use_smooth = True

            normals = mesh_data.get('normals')
            if normals and len(normals) == len(vertices):
                try:
                    # use_auto_smooth was removed in Blender 4.1; custom normals
                    # are honoured unconditionally from then on.
                    if hasattr(mesh, "use_auto_smooth"):
                        mesh.use_auto_smooth = True
                    mesh.normals_split_custom_set_from_vertices(normals)
                except Exception as e:
                    logger.error(f"  Failed to set custom normals: {e}")
            elif normals:
                logger.error(f"  Custom normals skipped: {len(normals)} normals for "
                             f"{len(vertices)} vertices")
            else:
                logger.log(f"  No normals in file for {obj_name}")

            if mesh_data.get('is_texture_paint') and mesh_data.get('layers'):
                mat = self._get_or_create_texture_paint_material(
                    mesh_data['layers'],
                    mesh_data.get('lightmap_texture', ''),
                    mesh_data,
                    importer
                )
            else:
                mat = self._get_or_create_material(
                    mesh_data.get('textures', []),
                    mesh_data.get('uv_indices', []),
                    mesh_data,
                    importer
                )
            
            if mat:
                obj.data.materials.append(mat)
                if mat.get(WEATHER_OVERLAY_PROP):
                    obj.hide_render = True
                    obj.hide_viewport = True
            
            logger.log(f"  Created object: {obj_name} with {len(vertices)} verts, {len(polygons)} faces")
            return obj
        
        except Exception as e:
            logger.error(f"  Failed to create mesh object: {e}")
            traceback.print_exc()
            return None

    def _material_built_here(self, mat_name, mesh_data):
        """A material this import already built, if there is one."""
        mat = _import_materials.get(mat_name)
        if mat is not None:
            apply_backface_culling(mat, mesh_data)
        return mat

    def _remember_material(self, mat_name, mat):
        if mat is not None:
            _import_materials[mat_name] = mat
        return mat

    def _get_or_create_material(self, textures, uv_indices, mesh_data, importer):
        # Check if this is a water material first
        shader_type = mesh_data.get('shader_type', '')
        water_params = mesh_data.get('water_params')
        
        if shader_type in CAUSTIC_SHADERS:
            params = mesh_data.get('material_params') or {}
            mat_name = f"Caustic_{params.get('name') or shader_type}"
            if self.time_of_day != 'NONE':
                mat_name += f"_{self.time_of_day}"
            mat = self._material_built_here(mat_name, mesh_data)
            if mat is not None:
                logger.log(f"  Using existing caustic material: {mat_name}")
                return mat
            logger.log(f"  Creating new caustic material: {mat_name}")
            mat = self._create_caustic_material(mesh_data, importer, mat_name)
            if mat is not None:
                return self._remember_material(mat_name, mat)

        if shader_type in ADDITIVE_SHADERS and textures:
            staged = material_stage_textures(mesh_data.get('material_params'), textures)
            mat_name = "Additive_" + "_".join(os.path.basename(t) for t in staged if t)
            mat_name += f"_{shader_type}"
            alpha = mesh_data.get('alpha')
            if alpha is not None:
                mat_name += f"_a{alpha:.2f}"
            mat = self._material_built_here(mat_name, mesh_data)
            if mat is not None:
                logger.log(f"  Using existing additive material: {mat_name}")
                return mat
            logger.log(f"  Creating new additive material: {mat_name}")
            mat = self._create_additive_material(textures, uv_indices, mesh_data,
                                                 importer, mat_name)
            if mat is not None:
                return self._remember_material(mat_name, mat)

        if importer.is_water_shader(shader_type) and water_params is not None:
            mat_name = "WaterMaterial"
            if water_params:
                if 'water_color' in water_params:
                    mat_name += f"_{water_params['water_color'][0]:.2f}"
            
            mat = self._material_built_here(mat_name, mesh_data)
            if mat is not None:
                logger.log(f"  Using existing water material: {mat_name}")
                return mat
            
            logger.log(f"  Creating new water material: {mat_name}")
            return self._remember_material(
                mat_name, self._create_water_material(mesh_data, importer, mat_name))
        
        if not textures:
            mat_name = "DefaultMaterial"
        else:
            uv_parts = []
            for i, (tex, uv_idx) in enumerate(zip(textures, uv_indices)):
                tex_base = os.path.basename(tex) if tex else "none"
                uv_parts.append(f"{tex_base}_UV{uv_idx}")
            mat_name = "_".join(uv_parts)

        # Two nodes can share a texture and still want different materials, so the key
        # carries how the material is built, not just what it samples.
        alpha_mode = resolve_alpha_mode(mesh_data)
        if shader_type:
            mat_name += f"_{shader_type}"
        if alpha_mode != ALPHA_CLIP:
            mat_name += f"_{alpha_mode}"
        if is_mirror_surface(mesh_data):
            mat_name += "_mirror"
        if window_glow_applies(mesh_data, self.time_of_day):
            mat_name += f"_lit{self.time_of_day}"
        # A mesh with no usable UVs cannot share a material with one that has
        # them: the shared UV node would name a layer this mesh never got.
        if not any(uvs and len(uvs) == len(mesh_data.get('vertices') or [])
                   for uvs in (mesh_data.get('uv_sets') or [])):
            mat_name += "_nouv"
        
        mat = self._material_built_here(mat_name, mesh_data)
        if mat is not None:
            logger.log(f"  Using existing material: {mat_name}")
            return mat
        
        logger.log(f"  Creating new material: {mat_name}")
        return self._remember_material(
            mat_name, self._create_material(textures, uv_indices, mesh_data, importer, mat_name))

    def _get_or_create_texture_paint_material(self, layers, lightmap_texture, mesh_data, importer):
        valid_layers = texture_paint_layers(mesh_data, importer)
        
        if not valid_layers:
            return None
        
        mat_name = "TexturePaint"
        for _, layer in valid_layers:
            mat_name += f"_{os.path.basename(layer['texture'])}"
        
        if lightmap_texture and self.time_of_day != 'NONE':
            lightmap_name = os.path.basename(lightmap_texture)
            mat_name += f"_LM_{lightmap_name}_{self.time_of_day}"
        
        mat = self._material_built_here(mat_name, mesh_data)
        if mat is not None:
            logger.log(f"  Using existing texture paint material: {mat_name}")
            return mat
        
        logger.log(f"  Creating new texture paint material: {mat_name}")
        return self._remember_material(
            mat_name,
            self._create_texture_paint_material_packed(layers, lightmap_texture, mesh_data,
                                                       importer, mat_name))

    def _create_material(self, textures, uv_indices, mesh_data, importer, mat_name=None):
        if not mat_name:
            mat_name = "DefaultMaterial"
        
        mat = bpy.data.materials.new(name=mat_name)
        mat.specular_intensity = 0.0
        apply_backface_culling(mat, mesh_data)
        
        mat.node_tree.nodes.clear()
        
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (800, 0)
        
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (600, 0)
        bsdf.inputs['Specular IOR Level'].default_value = 0.0

        # A mirror is the one case where the blanket "no specular" is wrong.
        if is_mirror_surface(mesh_data):
            bsdf.inputs['Specular IOR Level'].default_value = MIRROR_SPECULAR
            bsdf.inputs['Roughness'].default_value = MIRROR_ROUGHNESS
            mat.specular_intensity = MIRROR_SPECULAR
            logger.log(f"  Reflective surface (shader '{mesh_data.get('shader_type')}')")

        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        alpha_value = mesh_data.get('alpha')
        if alpha_value is None:
            alpha_value = 1.0
        else:
            try:
                alpha_value = min(max(float(alpha_value), 0.0), 1.0)
            except (TypeError, ValueError):
                alpha_value = 1.0
        
        alpha_mode = resolve_alpha_mode(mesh_data)
        apply_alpha_mode(mat, bsdf, alpha_mode, alpha_value)
        logger.log(f"  Alpha mode: {alpha_mode} (shader '{mesh_data.get('shader_type') or ''}')")
        
        if not textures:
            apply_alpha_mode(mat, bsdf, ALPHA_BLEND, UNTEXTURED_MATERIAL_ALPHA)
            bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
            return mat
        
        # Only slots the mesh actually filled get a UV node.
        vertex_count = len(mesh_data.get('vertices') or [])
        available_uvs = {i for i, uvs in enumerate(mesh_data.get('uv_sets') or [])
                         if uvs and len(uvs) == vertex_count}
        uv_nodes = {}
        for uv_idx in set(uv_indices):
            if available_uvs and uv_idx not in available_uvs:
                logger.log(f"  Texture names UV{uv_idx}, which this mesh has no UVs for")
                continue
            if not available_uvs:
                continue
            uv_name = "UVMap"
            if uv_idx > 0:
                uv_name = f"UVMap.{uv_idx}"
            
            uv_node = nodes.new('ShaderNodeUVMap')
            uv_node.location = (-800, 200 - (uv_idx * 200))
            uv_node.uv_map = uv_name
            uv_nodes[uv_idx] = uv_node
        
        texture_nodes = []
        diffuse_node = None
        lightmap_node = None
        normal_map_node = None
        lightmap_name = mesh_data.get('lightmap_texture')
        window_glow = window_glow_applies(mesh_data, self.time_of_day)
        
        # Look for normal map
        normal_map_texture = None
        normal_map_uv_idx = 0
        
        # A normal map is found by pairing: X has one when X_n exists on disk. A name
        # that merely ends in _n is not evidence of anything.
        for idx, tex_name in enumerate(textures):
            if not tex_name or tex_name.endswith('_n'):
                continue

            normal_candidate = tex_name + '_n'
            if importer.find_texture_file(normal_candidate):
                normal_map_texture = normal_candidate
                normal_map_uv_idx = uv_indices[idx] if idx < len(uv_indices) else 0
                logger.log(f"  Found matching normal map: {normal_candidate}")
                break
        
        # Process all textures
        for idx, tex_name in enumerate(textures):
            if not tex_name:
                continue

            if tex_name == normal_map_texture:
                continue
            
            tex_path = importer.find_texture_file(tex_name)
            if not tex_path:
                logger.log(f"  Texture not found: {tex_name}")
                continue
            
            uv_idx = uv_indices[idx] if idx < len(uv_indices) else 0
            
            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.location = (-500, 200 - (len(texture_nodes) * 200))
            tex_node.label = f"{os.path.basename(tex_name)}"
            
            try:
                img = bpy.data.images.load(tex_path)
                tex_node.image = img
                
                if (resolve_deferred_alpha_mode(alpha_mode, img, mesh_data) == ALPHA_OPAQUE
                        and not window_glow):
                    # Keep the mask out of the colour as well as out of coverage.
                    img.alpha_mode = 'NONE'
                
                if uv_idx in uv_nodes:
                    links.new(uv_nodes[uv_idx].outputs['UV'], tex_node.inputs['Vector'])
                
                texture_nodes.append((idx, tex_node, uv_idx))

                if lightmap_name and tex_name == lightmap_name:
                    lightmap_node = tex_node
                else:
                    diffuse_node = tex_node
                
            except Exception as e:
                logger.log(f"  Failed to load texture {tex_name}: {e}")
        
        # Load and setup normal map if found
        if normal_map_texture:
            normal_map_path = importer.find_texture_file(normal_map_texture)
            if normal_map_path:
                logger.log(f"  Loading normal map: {normal_map_path}")
                
                normal_uv_node = uv_nodes.get(normal_map_uv_idx)
                if normal_uv_node is None and normal_map_uv_idx in available_uvs:
                    uv_name = "UVMap"
                    if normal_map_uv_idx > 0:
                        uv_name = f"UVMap.{normal_map_uv_idx}"
                    normal_uv_node = nodes.new('ShaderNodeUVMap')
                    normal_uv_node.location = (-800, -400)
                    normal_uv_node.uv_map = uv_name
                    uv_nodes[normal_map_uv_idx] = normal_uv_node
                
                normal_tex_node = nodes.new('ShaderNodeTexImage')
                normal_tex_node.location = (-500, -400)
                normal_tex_node.label = f"Normal: {os.path.basename(normal_map_texture)}"
                
                try:
                    img = bpy.data.images.load(normal_map_path)
                    normal_tex_node.image = img
                    img.colorspace_settings.name = 'Non-Color'
                    
                    if normal_uv_node is not None:
                        links.new(normal_uv_node.outputs['UV'], normal_tex_node.inputs['Vector'])
                    
                    normal_map_node = nodes.new('ShaderNodeNormalMap')
                    normal_map_node.location = (-300, -400)
                    normal_map_node.space = 'TANGENT'
                    normal_map_node.inputs['Strength'].default_value = 1.0
                    
                    links.new(normal_tex_node.outputs['Color'], normal_map_node.inputs['Color'])
                    links.new(normal_map_node.outputs['Normal'], bsdf.inputs['Normal'])

                    logger.log(f"  Normal map connected successfully")

                except Exception as e:
                    logger.log(f"  Failed to load normal map {normal_map_texture}: {e}")
        
        # Handle material connections
        if not texture_nodes:
            bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
            return mat
        
        # Every object here sits at the world origin, so Blender sorts the blended sky
        # shells arbitrarily. Only the sun and moon need occluding.
        if (render_pass_tag(mesh_data) == 'SKY_'
                and (mesh_data.get('node_name') or '').startswith(CELESTIAL_NODE_PREFIX)
                and hasattr(mat, 'surface_render_method')):
            mat.surface_render_method = 'DITHERED'
            logger.log("  Sky body: writes depth so the shells can occlude it")

        # A node can be sorted into the transparent pass and still have nothing to blend.
        if (alpha_mode == ALPHA_BLEND and alpha_value >= 1.0
                and render_pass_tag(mesh_data) not in RENDER_PASS_EFFECT_LAYERS):
            blendable = diffuse_node or lightmap_node
            if blendable is not None and texture_alpha_is_trivial(blendable.image):
                logger.log("  Transparent pass, but the texture has no alpha to blend")
                alpha_mode = ALPHA_OPAQUE
                apply_alpha_mode(mat, bsdf, alpha_mode, 1.0)
                if (mesh_data.get('shader_type') or '') in WEATHER_CLOUD_SHADERS:
                    # Not an opaque cloud: a weather overlay with no weather to
                    # drive it. Keep the object, leave it out of the render.
                    mat[WEATHER_OVERLAY_PROP] = True
                    logger.log("  Weather layer would blank the sky; left hidden")

        # Check if we actually have a valid lightmap texture loaded
        has_lightmap = (lightmap_node is not None and 
                       self.time_of_day != 'NONE' and 
                       lightmap_node.image is not None)
        
        if has_lightmap:
            logger.log(f"  Creating lightmap setup for {self.time_of_day}")
            brightness_contrast = nodes.new('ShaderNodeBrightContrast')
            brightness_contrast.location = (-350, 100)
            brightness_contrast.label = "Lightmap Intensity"
            brightness_contrast.inputs['Bright'].default_value = 0.7
            brightness_contrast.inputs['Contrast'].default_value = 1.35
            
            links.new(lightmap_node.outputs['Color'], brightness_contrast.inputs['Color'])
            
            multiply_node = nodes.new('ShaderNodeMixRGB')
            multiply_node.location = (-200, 0)
            multiply_node.blend_type = 'MULTIPLY'
            multiply_node.label = "Apply Lightmap"
            multiply_node.inputs['Fac'].default_value = 1.0
            
            if diffuse_node:
                links.new(diffuse_node.outputs['Color'], multiply_node.inputs['Color1'])
                links.new(brightness_contrast.outputs['Color'], multiply_node.inputs['Color2'])
                links.new(multiply_node.outputs['Color'], bsdf.inputs['Base Color'])
                
                link_texture_alpha(links, nodes, diffuse_node, bsdf, alpha_mode, alpha_value,
                                   mesh_data)
            else:
                links.new(lightmap_node.outputs['Color'], bsdf.inputs['Base Color'])
                link_texture_alpha(links, nodes, lightmap_node, bsdf, alpha_mode, alpha_value,
                                   mesh_data)
        else:
            # No lightmap - simple diffuse connection
            logger.log(f"  No lightmap found, using simple diffuse connection")
            if diffuse_node:
                links.new(diffuse_node.outputs['Color'], bsdf.inputs['Base Color'])
                
                link_texture_alpha(links, nodes, diffuse_node, bsdf, alpha_mode, alpha_value,
                                   mesh_data)
            elif texture_nodes:
                # Use the first available texture
                tex_node = texture_nodes[0][1]
                links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                
                link_texture_alpha(links, nodes, tex_node, bsdf, alpha_mode, alpha_value,
                                   mesh_data)

        if window_glow:
            pane_node = diffuse_node or (texture_nodes[0][1] if texture_nodes else None)
            if pane_node is not None:
                self._add_window_glow(mat, bsdf, pane_node)

        return mat

    def _add_window_glow(self, mat, bsdf, pane_node):
        """Light a window's panes from inside."""
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        mask = nodes.new('ShaderNodeMath')
        mask.location = (200, -400)
        mask.operation = 'SUBTRACT'
        mask.label = "Pane mask"
        mask.inputs[0].default_value = 1.0
        links.new(pane_node.outputs['Alpha'], mask.inputs[1])

        strength = nodes.new('ShaderNodeMath')
        strength.location = (380, -400)
        strength.operation = 'MULTIPLY'
        strength.label = "Lamp strength"
        strength.inputs[1].default_value = WINDOW_GLOW_STRENGTH
        links.new(mask.outputs['Value'], strength.inputs[0])

        tint = nodes.new('ShaderNodeMixRGB')
        tint.location = (380, -600)
        tint.blend_type = 'MULTIPLY'
        tint.label = "Lamplight"
        tint.inputs['Fac'].default_value = 1.0
        tint.inputs['Color2'].default_value = (*WINDOW_GLOW_COLOR, 1.0)
        links.new(pane_node.outputs['Color'], tint.inputs['Color1'])

        emission = nodes.new('ShaderNodeEmission')
        emission.location = (600, -500)
        links.new(tint.outputs['Color'], emission.inputs['Color'])
        links.new(strength.outputs['Value'], emission.inputs['Strength'])

        add = nodes.new('ShaderNodeAddShader')
        add.location = (820, -200)
        links.new(bsdf.outputs['BSDF'], add.inputs[0])
        links.new(emission.outputs['Emission'], add.inputs[1])

        output = next((n for n in nodes if n.type == 'OUTPUT_MATERIAL'), None)
        if output is not None:
            links.new(add.outputs['Shader'], output.inputs['Surface'])
        logger.log("  Lit window panes from inside")

    def _create_caustic_material(self, mesh_data, importer, mat_name):
        """Build the caustic effect the game projects onto sewer and cave walls."""
        params = mesh_data.get('material_params') or {}
        textures = params.get('textures') or {}
        uv_index = params.get('uv_index', 1)

        tex_path = importer.find_texture_file(textures.get('tex', ''))
        if not tex_path:
            logger.log(f"  Caustic material has no '{textures.get('tex', '')}' texture, "
                       f"falling back to a plain material")
            return None

        mat = bpy.data.materials.new(name=mat_name)
        apply_backface_culling(mat, mesh_data)
        mat.blend_method = 'BLEND'
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'BLENDED'
        mat.node_tree.nodes.clear()
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        def uv_node_for(index, y):
            node = nodes.new('ShaderNodeUVMap')
            node.location = (-1100, y)
            node.uv_map = "UVMap" if index <= 0 else f"UVMap.{index}"
            return node

        def scrolled_texture(path, label, stage, y, non_color):
            uv = uv_node_for(uv_index, y)
            mapping = nodes.new('ShaderNodeMapping')
            mapping.location = (-900, y)
            mapping.label = f"Scroll {stage}"
            links.new(uv.outputs['UV'], mapping.inputs['Vector'])
            add_scroll_driver(mapping, *scroll_speeds(params, stage))

            tex = nodes.new('ShaderNodeTexImage')
            tex.location = (-700, y)
            tex.label = label
            tex.image = bpy.data.images.load(path)
            if non_color:
                tex.image.colorspace_settings.name = 'Non-Color'
            links.new(mapping.outputs['Vector'], tex.inputs['Vector'])
            return tex

        # Non-Color: the RGB is one flat tint and the game adds it to a gamma-space
        # framebuffer as authored.
        # Two scrolled copies of the same pattern, multiplied - the material's two
        # texture matrices.
        pattern = scrolled_texture(tex_path, os.path.basename(textures['tex']), 1, 200, True)
        pattern2 = scrolled_texture(tex_path, os.path.basename(textures['tex']), 2, -60, True)

        crossed = nodes.new('ShaderNodeMath')
        crossed.location = (-450, 120)
        crossed.operation = 'MULTIPLY'
        crossed.label = "Layer 1 x Layer 2"
        links.new(pattern.outputs['Alpha'], crossed.inputs[0])
        links.new(pattern2.outputs['Alpha'], crossed.inputs[1])
        strength = crossed.outputs['Value']

        # The mask is a pure function of V - every row of it is one value - and
        # it falls off over exactly the V range these meshes' UVs occupy. It is
        # the height falloff away from the water, and it does not scroll.
        mask_path = importer.find_texture_file(textures.get('mask', ''))
        if mask_path:
            mask_uv = uv_node_for(uv_index, -240)
            mask = nodes.new('ShaderNodeTexImage')
            mask.location = (-700, -240)
            mask.label = f"Falloff: {os.path.basename(textures['mask'])}"
            mask.image = bpy.data.images.load(mask_path)
            mask.image.colorspace_settings.name = 'Non-Color'
            links.new(mask_uv.outputs['UV'], mask.inputs['Vector'])

            faded = nodes.new('ShaderNodeMath')
            faded.location = (-300, 60)
            faded.operation = 'MULTIPLY'
            faded.label = "x Height Falloff"
            links.new(crossed.outputs['Value'], faded.inputs[0])
            links.new(mask.outputs['Alpha'], faded.inputs[1])
            strength = faded.outputs['Value']

        # Colour: the caustic's own tint, kept only where the level's caustic
        # lightmap says the water light actually falls.
        colour = pattern.outputs['Color']
        # The caustic shader's own lightmap, not one of the level's time-of-day bakes.
        lightmap_path = importer.find_texture_file(textures.get('lightmap', ''))
        if lightmap_path:
            lm_uv = uv_node_for(0, -400)
            lightmap = nodes.new('ShaderNodeTexImage')
            lightmap.location = (-700, -400)
            lightmap.label = f"Caustic lightmap: {os.path.basename(textures['lightmap'])}"
            lightmap.image = bpy.data.images.load(lightmap_path)
            links.new(lm_uv.outputs['UV'], lightmap.inputs['Vector'])

            tint = nodes.new('ShaderNodeMixRGB')
            tint.location = (-450, -200)
            tint.blend_type = 'MULTIPLY'
            tint.label = "Apply Caustic Lightmap"
            tint.inputs['Fac'].default_value = 1.0
            links.new(pattern.outputs['Color'], tint.inputs['Color1'])
            links.new(lightmap.outputs['Color'], tint.inputs['Color2'])
            colour = tint.outputs['Color']

        build_additive_output(nodes, links, colour, strength)
        return mat

    def _create_additive_material(self, textures, uv_indices, mesh_data, importer, mat_name):
        """Build a surface the game adds to the scene rather than covering it."""
        # Both stages sample the same UV set when the mesh carries only one, so
        # the extra texture needs no coordinates of its own.
        textures = material_stage_textures(mesh_data.get('material_params'), textures)
        paths = [(tex, importer.find_texture_file(tex)) for tex in textures if tex]
        paths = [(tex, path) for tex, path in paths if path]
        if not paths:
            return None

        mat = bpy.data.materials.new(name=mat_name)
        apply_backface_culling(mat, mesh_data)
        mat.blend_method = 'BLEND'
        if hasattr(mat, 'surface_render_method'):
            mat.surface_render_method = 'BLENDED'
        mat.node_tree.nodes.clear()
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links

        tex_nodes = []
        for i, (tex, path) in enumerate(paths):
            uv_idx = uv_indices[i] if i < len(uv_indices) else 0
            uv_node = nodes.new('ShaderNodeUVMap')
            uv_node.location = (-900, 200 - i * 300)
            uv_node.uv_map = "UVMap" if uv_idx <= 0 else f"UVMap.{uv_idx}"

            tex_node = nodes.new('ShaderNodeTexImage')
            tex_node.location = (-700, 200 - i * 300)
            tex_node.label = os.path.basename(tex)
            tex_node.image = bpy.data.images.load(path)
            links.new(uv_node.outputs['UV'], tex_node.inputs['Vector'])
            tex_nodes.append(tex_node)

        colour = tex_nodes[0].outputs['Color']
        for i, extra in enumerate(tex_nodes[1:]):
            mix = nodes.new('ShaderNodeMixRGB')
            mix.location = (-450, 100 - i * 150)
            mix.blend_type = 'MULTIPLY'
            mix.label = "Stage %d" % (i + 2)
            mix.inputs['Fac'].default_value = 1.0
            links.new(colour, mix.inputs['Color1'])
            links.new(extra.outputs['Color'], mix.inputs['Color2'])
            colour = mix.outputs['Color']

        # The stages' alphas multiply: the plume decides where the effect is, the detail
        # sheet only what it looks like there.
        strength = None
        for i, tex_node in enumerate(tex_nodes):
            if tex_node.image is None or tex_node.image.channels < 4:
                continue
            if strength is None:
                strength = tex_node.outputs['Alpha']
                continue
            mul = nodes.new('ShaderNodeMath')
            mul.location = (-450, -300 - i * 150)
            mul.operation = 'MULTIPLY'
            mul.label = "Stage alphas"
            links.new(strength, mul.inputs[0])
            links.new(tex_node.outputs['Alpha'], mul.inputs[1])
            strength = mul.outputs['Value']

        alpha_value = mesh_data.get('alpha')
        if alpha_value is not None:
            try:
                alpha_value = min(max(float(alpha_value), 0.0), 1.0)
            except (TypeError, ValueError):
                alpha_value = 1.0
        else:
            alpha_value = 1.0

        emission = build_additive_output(nodes, links, colour, strength)
        if alpha_value < 1.0:
            if strength is None:
                emission.inputs['Strength'].default_value = alpha_value
            else:
                scale = nodes.new('ShaderNodeMath')
                scale.location = (-450, -150)
                scale.operation = 'MULTIPLY'
                scale.label = "Node Alpha"
                scale.inputs[1].default_value = alpha_value
                links.new(strength, scale.inputs[0])
                links.new(scale.outputs['Value'], emission.inputs['Strength'])
        return mat

    def _create_texture_paint_material_packed(self, layers, lightmap_texture, mesh_data, importer, mat_name=None):
        valid_layers = texture_paint_layers(mesh_data, importer)
        
        if not valid_layers:
            return None
        
        if not mat_name:
            mat_name = "TexturePaint"
            for _, layer in valid_layers:
                mat_name += f"_{os.path.basename(layer['texture'])}"
            if lightmap_texture and self.time_of_day != 'NONE':
                mat_name += f"_lm_{self.time_of_day}"
        
        mat = self._material_built_here(mat_name, mesh_data)
        if mat is not None:
            return mat
        
        mat = bpy.data.materials.new(name=mat_name)
        mat.specular_intensity = 0.0
        apply_backface_culling(mat, mesh_data)
        
        mat.node_tree.nodes.clear()
        
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (2000, 0)
        
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (1800, 0)
        bsdf.inputs['Specular IOR Level'].default_value = 0.0
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        alpha_value = mesh_data.get('alpha')
        if alpha_value is None:
            alpha_value = 1.0
        else:
            try:
                alpha_value = min(max(float(alpha_value), 0.0), 1.0)
            except (TypeError, ValueError):
                alpha_value = 1.0
        
        apply_alpha_mode(mat, bsdf, resolve_alpha_mode(mesh_data), alpha_value)
        
        uv_base = nodes.new('ShaderNodeUVMap')
        uv_base.location = (-2000, 400)
        uv_base.uv_map = "UVMap"
        uv_base.label = "Base UVs"
        
        # The paint UV set already carries the terrain's own tiling, so it is
        # used as-is. The identity Mapping node is kept purely as a handle for
        # anyone who wants to retile a level by hand.
        mapping = nodes.new('ShaderNodeMapping')
        mapping.location = (-1700, 400)
        mapping.vector_type = 'POINT'
        mapping.label = "Paint Tiling"
        links.new(uv_base.outputs['UV'], mapping.inputs['Vector'])
        
        layer_batches = []
        for i in range(0, len(valid_layers), 3):
            layer_batches.append(valid_layers[i:i+3])
        
        texture_nodes = []
        current_x = -1400
        
        for batch_idx, batch in enumerate(layer_batches):
            for sub_idx, (orig_idx, layer) in enumerate(batch):
                tex_path = importer.find_texture_file(layer['texture'])
                if not tex_path:
                    continue
                
                tex_node = nodes.new('ShaderNodeTexImage')
                tex_node.location = (current_x, 600 - (sub_idx * 300))
                tex_node.label = f"Layer {orig_idx}: {os.path.basename(layer['texture'])}"
                
                try:
                    img = bpy.data.images.load(tex_path)
                    tex_node.image = img
                    img.alpha_mode = 'NONE'
                    
                    links.new(mapping.outputs['Vector'], tex_node.inputs['Vector'])
                    texture_nodes.append((orig_idx, tex_node, batch_idx, sub_idx))
                except Exception as e:
                    logger.log(f"    Failed to load texture {layer['texture']}: {e}")
            
            current_x += 300
        
        if not texture_nodes:
            bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
            return mat
        
        color_nodes = []
        for batch_idx in range(len(layer_batches)):
            color_attr = nodes.new('ShaderNodeVertexColor')
            color_attr.location = (-1600, -200 - (batch_idx * 200))
            color_attr.layer_name = f"Weights_{batch_idx}"
            color_attr.label = f"Weights Batch {batch_idx}"
            color_nodes.append(color_attr)
        
        separate_rgb_nodes = []
        for batch_idx, color_node in enumerate(color_nodes):
            separate_rgb = nodes.new('ShaderNodeSeparateColor')
            separate_rgb.location = (-1300, -200 - (batch_idx * 200))
            separate_rgb.mode = 'RGB'
            links.new(color_node.outputs['Color'], separate_rgb.inputs['Color'])
            separate_rgb_nodes.append(separate_rgb)
        
        texture_weight_map = []
        for orig_idx, tex_node, batch_idx, sub_idx in texture_nodes:
            if sub_idx == 0:
                weight_source = separate_rgb_nodes[batch_idx].outputs['Red']
            elif sub_idx == 1:
                weight_source = separate_rgb_nodes[batch_idx].outputs['Green']
            else:
                weight_source = separate_rgb_nodes[batch_idx].outputs['Blue']
            texture_weight_map.append((tex_node, weight_source, orig_idx))
        
        texture_weight_map.sort(key=lambda x: x[2])
        
        # Splatting is a weighted sum and the file's weights already add to 1 per vertex.
        # An ADD mix accumulates that sum; chained MIX nodes do not.
        current_output = None
        weight_total = None
        current_x = -1000

        for idx, (tex_node, weight_source, orig_idx) in enumerate(texture_weight_map):
            mix_node = nodes.new('ShaderNodeMixRGB')
            mix_node.location = (current_x, 0)
            mix_node.blend_type = 'ADD'
            mix_node.label = f"+ Layer {orig_idx}"

            if current_output:
                links.new(current_output, mix_node.inputs['Color1'])
            else:
                mix_node.inputs['Color1'].default_value = (0.0, 0.0, 0.0, 1.0)

            links.new(tex_node.outputs['Color'], mix_node.inputs['Color2'])
            links.new(weight_source, mix_node.inputs['Fac'])

            current_output = mix_node.outputs['Color']

            # Run the same sum over the weights themselves, to divide by below.
            if weight_total is None:
                weight_total = weight_source
            else:
                add = nodes.new('ShaderNodeMath')
                add.operation = 'ADD'
                add.location = (current_x, -560)
                add.label = f"Weight sum {orig_idx}"
                links.new(weight_total, add.inputs[0])
                links.new(weight_source, add.inputs[1])
                weight_total = add.outputs['Value']

            current_x += 300

        # Blank layers carry weight too, so normalise by the weight actually summed.
        #
        if current_output is not None and weight_total is not None:
            safe_total = nodes.new('ShaderNodeMath')
            safe_total.operation = 'MAXIMUM'
            safe_total.location = (current_x, -560)
            safe_total.label = "Avoid divide by zero"
            safe_total.inputs[1].default_value = 1e-3
            links.new(weight_total, safe_total.inputs[0])

            normalize = nodes.new('ShaderNodeVectorMath')
            normalize.operation = 'DIVIDE'
            normalize.location = (current_x + 200, -200)
            normalize.label = "Normalise by painted weight"
            links.new(current_output, normalize.inputs[0])
            links.new(safe_total.outputs['Value'], normalize.inputs[1])

            painted = nodes.new('ShaderNodeMath')
            painted.operation = 'MULTIPLY'
            painted.location = (current_x + 200, -560)
            painted.label = "Is anything painted?"
            painted.inputs[1].default_value = 20.0
            painted.use_clamp = True
            links.new(weight_total, painted.inputs[0])

            fallback = nodes.new('ShaderNodeMixRGB')
            fallback.blend_type = 'MIX'
            fallback.location = (current_x + 450, 0)
            fallback.label = "Unpainted -> first layer"
            links.new(texture_weight_map[0][0].outputs['Color'], fallback.inputs['Color1'])
            links.new(normalize.outputs['Vector'], fallback.inputs['Color2'])
            links.new(painted.outputs['Value'], fallback.inputs['Fac'])

            current_output = fallback.outputs['Color']
            current_x += 700
        
        # Check if we actually have a valid lightmap
        has_lightmap = False
        lightmap_path = None
        
        if lightmap_texture and self.time_of_day != 'NONE':
            lightmap_path = importer.find_texture_file(lightmap_texture)
            if lightmap_path and os.path.exists(lightmap_path):
                has_lightmap = True
        
        if has_lightmap and lightmap_path:
            logger.log(f"  Creating lightmap setup for texture paint")
            uv_lightmap = nodes.new('ShaderNodeUVMap')
            uv_lightmap.location = (-2000, -400)
            uv_lightmap.uv_map = "LightmapUV"
            uv_lightmap.label = "Lightmap UVs"
            
            lightmap_node = nodes.new('ShaderNodeTexImage')
            lightmap_node.location = (current_x, 200)
            lightmap_node.label = f"Lightmap {self.time_of_day}"
            
            try:
                img = bpy.data.images.load(lightmap_path)
                lightmap_node.image = img
                links.new(uv_lightmap.outputs['UV'], lightmap_node.inputs['Vector'])
                
                brightness_contrast = nodes.new('ShaderNodeBrightContrast')
                brightness_contrast.location = (current_x + 150, 150)
                brightness_contrast.label = "Lightmap Intensity"
                brightness_contrast.inputs['Bright'].default_value = 0.7
                brightness_contrast.inputs['Contrast'].default_value = 1.35
                
                links.new(lightmap_node.outputs['Color'], brightness_contrast.inputs['Color'])
                
                lightmap_mix = nodes.new('ShaderNodeMixRGB')
                lightmap_mix.location = (current_x + 450, 100)
                lightmap_mix.blend_type = 'MULTIPLY'
                lightmap_mix.label = "Apply Lightmap"
                lightmap_mix.inputs['Fac'].default_value = 1.0
                
                if current_output:
                    links.new(current_output, lightmap_mix.inputs['Color1'])
                else:
                    lightmap_mix.inputs['Color1'].default_value = (1.0, 1.0, 1.0, 1.0)
                
                links.new(brightness_contrast.outputs['Color'], lightmap_mix.inputs['Color2'])
                links.new(lightmap_mix.outputs['Color'], bsdf.inputs['Base Color'])
                
            except Exception as e:
                logger.log(f"    Failed to load lightmap: {e}")
                if current_output:
                    links.new(current_output, bsdf.inputs['Base Color'])
                else:
                    bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
        else:
            logger.log(f"  No lightmap found for texture paint, using color mix only")
            if current_output:
                links.new(current_output, bsdf.inputs['Base Color'])
            else:
                bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
        
        return mat

# ---------------------------------------------------------------------------
# .mba animation import
# ---------------------------------------------------------------------------

class MBAAnimation:
    """One animation entry inside a .mba pack."""

    def __init__(self):
        self.name = ""
        self.length = 0.0
        self.transition_time = 0.0
        self.root_name = ""
        self.root_node_offset = 0


class MBAImporter:
    """Reads a .mba animation pack."""

    def __init__(self, filepath):
        self.filepath = filepath
        self.reader = BinaryReader(filepath)
        self.model_data = ModelData()
        self.model_name = ""
        self.animations = []

    def close(self):
        if self.reader:
            self.reader.close()
            self.reader = None

    def read_header(self):
        if self.reader.read_u8() != 0:
            raise ValueError("Not a binary Witcher .mba file")

        self.reader.seek(4)
        version = self.reader.read_u32() & 0x0FFFFFFF
        self.model_data.file_version = version
        if version not in (FILE_VERSION_133, FILE_VERSION_136):
            raise ValueError(f"Unsupported .mba version: {version}")

        model_count = self.reader.read_u32()
        if model_count != 1:
            raise ValueError(f"Unsupported model count: {model_count}")

        self.reader.seek(4, 1)
        self.model_data.size_model_data = self.reader.read_u32()
        self.reader.seek(4, 1)
        self.model_data.offset_model_data = 32

        if version == FILE_VERSION_133:
            self.model_data.offset_raw_data = self.reader.read_u32() + 32
            self.model_data.size_raw_data = self.reader.read_u32()
            self.model_data.offset_tex_data = 32
        else:
            self.model_data.offset_raw_data = 32
            self.model_data.offset_tex_data = self.reader.read_u32() + 32
            self.model_data.size_tex_data = self.reader.read_u32()

        self.reader.seek(8, 1)
        self.model_name = self.reader.read_string(64)
        logger.log(f"Animation pack: {self.model_name} (version {version})", force=True)

    def read_animation_list(self):
        if self.model_data.file_version == FILE_VERSION_133:
            chunk_start = self.model_data.offset_raw_data
        else:
            chunk_start = self.model_data.offset_tex_data

        self.reader.seek(chunk_start)
        self.reader.seek(4, 1)
        anim_array = ArrayDef.read(self.reader)

        self.reader.seek(chunk_start + anim_array.first_elem_offset)
        offsets = [self.reader.read_u32() for _ in range(anim_array.nb_used_entries)]

        self.animations = []
        for offset in offsets:
            self.reader.seek(self.model_data.offset_model_data + offset)

            # Geometry header
            self.reader.seek(8, 1)
            name = self.reader.read_string(64)
            root_node_offset = self.reader.read_u32()
            self.reader.seek(32, 1)
            self.reader.seek(4, 1)          # geometry type + padding

            # Animation header
            animation = MBAAnimation()
            animation.name = name
            animation.root_node_offset = root_node_offset
            animation.length = self.reader.read_f32()
            animation.transition_time = self.reader.read_f32()
            animation.root_name = self.reader.read_string(64)
            self.animations.append(animation)

        logger.log(f"Found {len(self.animations)} animations", force=True)
        return self.animations

    def _read_u32_array(self, array_def):
        if array_def.nb_used_entries == 0:
            return []
        pos = self.reader.tell()
        self.reader.seek(self.model_data.offset_model_data + array_def.first_elem_offset)
        values = [self.reader.read_u32() for _ in range(array_def.nb_used_entries)]
        self.reader.seek(pos)
        return values

    def _read_f32_array(self, array_def):
        if array_def.nb_used_entries == 0:
            return []
        pos = self.reader.tell()
        self.reader.seek(self.model_data.offset_model_data + array_def.first_elem_offset)
        values = [self.reader.read_f32() for _ in range(array_def.nb_used_entries)]
        self.reader.seek(pos)
        return values

    def read_animation_nodes(self, animation):
        """Read one animation's node tree.

        Returns ({node_name: {channel: (times, values)}}, {node_name: parent}).
        """
        nodes = {}
        parents = {}
        self._read_animation_node(animation.root_node_offset, nodes, parents,
                                  None, animation)
        return nodes, parents

    def _read_animation_node(self, node_offset, nodes, parents, parent_name,
                             animation):
        self.reader.seek(self.model_data.offset_model_data + node_offset)

        self.reader.seek(24, 1)             # function pointers
        self.reader.seek(4, 1)              # inherit colour flag
        self.reader.read_u32()              # node id
        node_name = self.reader.read_string(64)
        self.reader.seek(8, 1)              # parent geometry + parent node

        children_def = ArrayDef.read(self.reader)
        children = self._read_u32_array(children_def)

        key_def = ArrayDef.read(self.reader)
        data_def = ArrayDef.read(self.reader)
        data = self._read_f32_array(data_def)

        channels = self._read_channels(key_def, data, node_name, animation)
        named = bool(node_name) and node_name != "NULL"
        if named:
            parents[node_name] = parent_name
            if channels:
                nodes[node_name] = channels

        for child_offset in children:
            self._read_animation_node(child_offset, nodes, parents,
                                      node_name if named else parent_name,
                                      animation)

    def _read_channels(self, key_def, data, node_name, animation):
        channels = {}
        if key_def.nb_used_entries == 0:
            return channels

        self.reader.seek(self.model_data.offset_model_data + key_def.first_elem_offset)
        headers = []
        for _ in range(key_def.nb_used_entries):
            controller_type = self.reader.read_u32()
            row_count = self.reader.read_u16()
            time_index = self.reader.read_u16()
            data_index = self.reader.read_u16()
            packed_columns = self.reader.read_u8()
            self.reader.seek(1, 1)          # padding
            column_count = packed_columns & CONTROLLER_COLUMN_MASK
            row_stride = column_count * CONTROLLER_ROW_SETS.get(
                packed_columns & ~CONTROLLER_COLUMN_MASK, 1)
            headers.append((controller_type, row_count, time_index,
                            data_index, column_count, row_stride))

        # A value block may not run into the next block along: where it does, the file
        # disagrees with itself.
        block_starts = sorted({h[2] for h in headers} | {h[3] for h in headers})

        for (controller_type, row_count, time_index, data_index,
                column_count, row_stride) in headers:
            if row_count == 0 or row_count == 0xFFFF or column_count == 0:
                continue

            last_value = data_index + row_count * row_stride
            next_block = min([s for s in block_starts if s > data_index],
                             default=len(data))
            if (time_index + row_count > len(data) or last_value > len(data)
                    or last_value > next_block):
                logger.error(f"  {animation.name}/{node_name}: controller "
                             f"{controller_type} runs past its data array, skipped")
                continue

            times = [data[time_index + j] for j in range(row_count)]

            if controller_type == CONTROLLER_POSITION and column_count >= 3:
                values = []
                for j in range(row_count):
                    base = data_index + j * row_stride
                    values.append(Vector((data[base], data[base + 1], data[base + 2])))
                channels['position'] = (times, values)

            elif controller_type == CONTROLLER_ORIENTATION and column_count >= 4:
                values = []
                for j in range(row_count):
                    base = data_index + j * row_stride
                    # Stored xyzw, mathutils wants wxyz.
                    values.append(Quaternion((data[base + 3], data[base],
                                              data[base + 1], data[base + 2])))
                channels['rotation'] = (times, values)

            elif controller_type == CONTROLLER_SCALE:
                values = []
                for j in range(row_count):
                    s = data[data_index + j * row_stride]
                    values.append(Vector((s, s, s)))
                channels['scale'] = (times, values)

        return channels


def _sample_segment(times, t):
    """Return (index, blend) for sampling a key list at time t."""
    count = len(times)
    if count == 1 or t <= times[0]:
        return 0, 0.0
    if t >= times[-1]:
        return count - 1, 0.0
    i = bisect.bisect_right(times, t) - 1
    i = min(max(i, 0), count - 2)
    span = times[i + 1] - times[i]
    if span <= 1e-9:
        return i, 0.0
    return i, (t - times[i]) / span


def _sample_vector(channel, t, fallback):
    if not channel:
        return fallback
    times, values = channel
    i, blend = _sample_segment(times, t)
    if blend == 0.0:
        return values[i]
    return values[i].lerp(values[i + 1], blend)


def _sample_quaternion(channel, t, fallback):
    if not channel:
        return fallback
    times, values = channel
    i, blend = _sample_segment(times, t)
    if blend == 0.0:
        return values[i]
    return values[i].slerp(values[i + 1], blend)


def _bone_rest_frames(armature_obj):
    """Per-bone constants that map node-space animation onto pose bones."""
    frames = {}
    for bone in armature_obj.data.bones:
        rest_local = bone.get(REST_LOCAL_PROP)
        rest_global = bone.get(REST_GLOBAL_PROP)
        if rest_local is None or rest_global is None:
            continue

        local = list_to_matrix(list(rest_local))
        global_matrix = list_to_matrix(list(rest_global))
        change_of_basis = global_matrix.inverted() @ bone.matrix_local

        frames[bone.name] = {
            'rest_local': local,
            'rest_local_inv': local.inverted(),
            'basis': change_of_basis,
            'basis_inv': change_of_basis.inverted(),
            'rest_trs': local.decompose(),
            # Where this bone's parent node sits, for walking a chain that runs
            # off the top of the armature.
            'parent_global': global_matrix @ local.inverted(),
        }
    return frames


def _action_channels(action, armature_obj):
    """Create the channel container of a fresh action, returning (bag, slot)."""
    slot = action.slots.new(id_type='OBJECT', name=armature_obj.name)
    layer = action.layers.new("Layer")
    strip = layer.strips.new(type='KEYFRAME')
    return strip.channelbag(slot, ensure=True), slot


def _write_fcurve(channelbag, data_path, index, frames, values, group):
    fcurve = channelbag.fcurves.new(data_path, index=index)
    if group is not None:
        try:
            fcurve.group = group
        except Exception:
            pass
    points = fcurve.keyframe_points
    points.add(len(frames))
    flat = []
    for frame, value in zip(frames, values):
        flat.append(frame)
        flat.append(value)
    points.foreach_set("co", flat)
    for point in points:
        point.interpolation = 'LINEAR'
    fcurve.update()


def _write_channel_set(channelbag, data_path, frames, values, size, group):
    """Write one vector/quaternion channel, collapsing constant curves to one key."""
    for index in range(size):
        component = [v[index] for v in values]
        first = component[0]
        if all(abs(c - first) <= 1e-7 for c in component):
            _write_fcurve(channelbag, data_path, index, frames[:1], component[:1], group)
        else:
            _write_fcurve(channelbag, data_path, index, frames, component, group)


def _is_placement_root(bone, animated_names):
    """True when no ancestor of this bone is animated too."""
    parent = bone.parent
    while parent is not None:
        if parent.name in animated_names:
            return False
        parent = parent.parent
    return True


class _NodePoses:
    """Evaluates an animation in both hierarchies at once."""

    def __init__(self, armature_obj, nodes, parents, rest_frames):
        self.bones = armature_obj.data.bones
        self.nodes = nodes
        self.rest_frames = rest_frames
        self.effective = {}
        self.parents = self._align_roots(parents, armature_obj)
        self.reparented = self._find_reparented()
        self._anim_cache = {}
        self._model_cache = {}

    def _align_roots(self, parents, armature_obj):
        """Rename the pack's root to the armature's, so the trees start level."""
        anim_roots = [n for n, p in parents.items() if p is None]
        bone_roots = [b.name for b in self.bones if b.parent is None]
        if len(anim_roots) != 1 or len(bone_roots) != 1:
            return dict(parents)
        anim_root, bone_root = anim_roots[0], bone_roots[0]
        if anim_root == bone_root or anim_root in self.bones:
            return dict(parents)
        aligned = {(bone_root if n == anim_root else n):
                   (bone_root if p == anim_root else p)
                   for n, p in parents.items()}
        if anim_root in self.nodes:
            self.nodes[bone_root] = self.nodes.pop(anim_root)
        return aligned

    def _find_reparented(self):
        """Bones whose parent in the pack is not their parent in the model."""
        reparented = set()
        for bone in self.bones:
            if bone.name not in self.parents:
                continue
            # The pack may route through dummies the model has no bone for.
            pack_parent = self.parents.get(bone.name)
            while pack_parent is not None and pack_parent not in self.bones:
                pack_parent = self.parents.get(pack_parent)
            own_parent = bone.parent.name if bone.parent else None
            if pack_parent != own_parent:
                reparented.add(bone.name)
        return reparented

    def set_channels(self, bone_name, position, rotation, scale):
        self.effective[bone_name] = (position, rotation, scale)

    def node_local(self, name, t):
        """A node's own transform at time t, local to its parent in the pack."""
        frame = self.rest_frames.get(name)
        channels = self.effective.get(name)
        if channels is None:
            if name in self.nodes:
                channels = (self.nodes[name].get('position'),
                            self.nodes[name].get('rotation'),
                            self.nodes[name].get('scale'))
            elif frame is not None:
                return frame['rest_local']
            else:
                return Matrix.Identity(4)
        if frame is not None:
            rest = frame['rest_trs']
        else:
            rest = (Vector((0.0, 0.0, 0.0)), Quaternion(), Vector((1.0, 1.0, 1.0)))
        position, rotation, scale = channels
        return (Matrix.Translation(_sample_vector(position, t, rest[0]))
                @ _sample_quaternion(rotation, t, rest[1]).to_matrix().to_4x4()
                @ Matrix.Diagonal(_sample_vector(scale, t, rest[2])).to_4x4())

    def _base(self, name):
        frame = self.rest_frames.get(name)
        return frame['parent_global'] if frame else Matrix.Identity(4)

    def anim_global(self, name, t, depth=0):
        """Where the node ends up, composed down the pack's own tree."""
        key = (name, t)
        cached = self._anim_cache.get(key)
        if cached is not None:
            return cached
        matrix = self.node_local(name, t)
        parent = self.parents.get(name)
        if parent is not None and depth < 64:
            matrix = self.anim_global(parent, t, depth + 1) @ matrix
        else:
            matrix = self._base(name) @ matrix
        self._anim_cache[key] = matrix
        return matrix

    def model_local(self, name, t):
        """The same transform, local to the parent the model gives the node."""
        if name not in self.reparented:
            return self.node_local(name, t)
        bone = self.bones.get(name)
        if bone is None:
            return self.node_local(name, t)
        if bone.parent is not None:
            parent_global = self.model_global(bone.parent.name, t)
        else:
            parent_global = self._base(name)
        return parent_global.inverted() @ self.anim_global(name, t)

    def model_global(self, name, t, depth=0):
        key = (name, t)
        cached = self._model_cache.get(key)
        if cached is not None:
            return cached
        matrix = self.model_local(name, t)
        bone = self.bones.get(name)
        if bone is not None and bone.parent is not None and depth < 64:
            matrix = self.model_global(bone.parent.name, t, depth + 1) @ matrix
        else:
            matrix = self._base(name) @ matrix
        self._model_cache[key] = matrix
        return matrix


def _animation_closes(nodes, length):
    """True when the animation's last key repeats its first pose."""
    if length <= 0.0:
        return False
    closed = False
    for channels in nodes.values():
        for kind, channel in channels.items():
            times, values = channel
            if len(times) < 2:
                continue
            if kind == 'rotation':
                first = _sample_quaternion(channel, 0.0, values[0])
                last = _sample_quaternion(channel, length, values[-1])
                # A quaternion and its negation are the same orientation.
                delta = min((first - last).magnitude, (first + last).magnitude)
            else:
                first = _sample_vector(channel, 0.0, values[0])
                last = _sample_vector(channel, length, values[-1])
                delta = (first - last).magnitude
            if delta > ANIMATION_LOOP_TOLERANCE:
                return False
            closed = True
    return closed


def build_animation_action(armature_obj, nodes, parents, animation, rest_frames,
                           fps, action_name):
    """Turn one parsed animation into a Blender action on armature_obj."""
    action = bpy.data.actions.new(action_name)
    channelbag, slot = _action_channels(action, armature_obj)

    poses = _NodePoses(armature_obj, nodes, parents, rest_frames)
    animated_names = set(nodes) & set(rest_frames)

    # Resolve every node's channels before sampling any of them: a reparented
    # node is placed against its parent's animated position, so the parent's own
    # channels have to be settled first.
    for bone_name, channels in nodes.items():
        if bone_name not in rest_frames:
            continue
        position = channels.get('position')
        # A reparented node keeps its stored position even when it never moves: it is a
        # placement in the pack's frame, not a bone offset in the model's.
        if (position and len(position[0]) < 2
                and bone_name not in poses.reparented
                and not _is_placement_root(armature_obj.data.bones[bone_name],
                                           animated_names)):
            position = None
        poses.set_channels(bone_name, position, channels.get('rotation'),
                           channels.get('scale'))

    keyed_bones = 0
    for bone_name, channels in nodes.items():
        frame_data = rest_frames.get(bone_name)
        if frame_data is None:
            continue

        position, rotation, scale = poses.effective[bone_name]

        # A node may animate only some of its channels; the others keep the
        # value the model's rest pose gave them.
        rest_position, rest_rotation, rest_scale = frame_data['rest_trs']

        sample_times = set()
        for channel in (position, rotation, scale):
            if channel:
                sample_times.update(channel[0])
        sample_times = sorted(sample_times)
        if not sample_times:
            continue

        rest_local_inv = frame_data['rest_local_inv']
        change_of_basis = frame_data['basis']
        change_of_basis_inv = frame_data['basis_inv']

        frames = []
        locations = []
        quaternions = []
        scales = []
        previous_quaternion = None

        reparented = bone_name in poses.reparented
        for t in sample_times:
            if reparented:
                local_anim = poses.model_local(bone_name, t)
            else:
                pos = _sample_vector(position, t, rest_position)
                rot = _sample_quaternion(rotation, t, rest_rotation)
                scl = _sample_vector(scale, t, rest_scale)
                local_anim = (Matrix.Translation(pos) @ rot.to_matrix().to_4x4()
                              @ Matrix.Diagonal(scl).to_4x4())
            basis = change_of_basis_inv @ rest_local_inv @ local_anim @ change_of_basis

            loc, quat, scl_out = basis.decompose()
            # Keep successive quaternions in the same hemisphere so Blender's
            # per-component interpolation takes the short way round.
            if previous_quaternion is not None and quat.dot(previous_quaternion) < 0.0:
                quat = -quat
            previous_quaternion = quat

            frame = 1.0 + t * fps
            if abs(frame - round(frame)) < ANIMATION_FRAME_SNAP:
                frame = float(round(frame))
            frames.append(frame)
            locations.append(loc)
            quaternions.append(quat)
            scales.append(scl_out)

        try:
            group = channelbag.groups.new(bone_name)
        except Exception:
            group = None

        path = f'pose.bones["{bone_name}"]'
        _write_channel_set(channelbag, path + '.location', frames, locations, 3, group)
        _write_channel_set(channelbag, path + '.rotation_quaternion', frames,
                           quaternions, 4, group)
        _write_channel_set(channelbag, path + '.scale', frames, scales, 3, group)
        keyed_bones += 1

    action.use_fake_user = True
    detail = ""
    if poses.reparented:
        detail = (f", {len(poses.reparented)} nodes the pack parents "
                  f"differently from the model")
    logger.log(f"  {animation.name}: {keyed_bones} bones, "
               f"{animation.length:.2f}s{detail}")
    return action, slot, keyed_bones


class IMPORT_MBA_OT_operator(Operator, ImportHelper):
    """Import a Witcher .mba animation pack onto an imported skeleton"""
    bl_idname = "import_scene.mba"
    bl_label = "Import Witcher Animation"
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".mba"
    filter_glob: StringProperty(default="*.mba", options={'HIDDEN'})

    armature_name: StringProperty(
        name="Armature",
        description="Armature to animate. Leave empty to use the active armature, "
                    "or the only armature in the scene",
        default="",
    )

    animation_filter: StringProperty(
        name="Name Filter",
        description="Only import animations whose name contains one of these "
                    "comma-separated substrings. Leave empty to import all",
        default="",
    )

    max_animations: IntProperty(
        name="Max Animations",
        description="Stop after this many animations (0 = no limit). A full pack "
                    "can hold well over a hundred",
        default=0,
        min=0,
    )

    match_scene_frame_rate: BoolProperty(
        name="Set Scene Frame Rate",
        description="Set the scene to the 30 fps the packs are authored at, so "
                    "the animation plays at the speed it was made for",
        default=True,
    )

    assign_first: BoolProperty(
        name="Assign First Animation",
        description="Assign the first imported animation to the armature and set "
                    "the scene frame range to match",
        default=True,
    )

    push_to_nla: BoolProperty(
        name="Push To NLA",
        description="Store every imported animation as its own muted NLA track",
        default=False,
    )

    debug_mode: BoolProperty(
        name="Debug Mode",
        description="Enable detailed logging",
        default=False,
    )

    def _find_armature(self, context):
        if self.armature_name:
            obj = bpy.data.objects.get(self.armature_name)
            if obj is None or obj.type != 'ARMATURE':
                return None, f"No armature named '{self.armature_name}'"
            return obj, None

        active = context.view_layer.objects.active
        if active is not None and active.type == 'ARMATURE':
            return active, None

        armatures = [o for o in context.scene.objects if o.type == 'ARMATURE']
        if len(armatures) == 1:
            return armatures[0], None
        if not armatures:
            return None, ("No armature in the scene - import a .mdb with "
                          "'Import Skeletons' first")
        return None, ("Several armatures in the scene - make one active or fill in "
                      "the Armature field")

    def execute(self, context):
        global logger
        logger.enabled = self.debug_mode

        armature_obj, error = self._find_armature(context)
        if armature_obj is None:
            self.report({'ERROR'}, error)
            return {'CANCELLED'}

        rest_frames = _bone_rest_frames(armature_obj)
        if not rest_frames:
            self.report({'ERROR'},
                        f"'{armature_obj.name}' carries no Witcher rest data on its "
                        f"bones. Re-import the .mdb with 'Import Skeletons' enabled")
            return {'CANCELLED'}

        importer = None
        try:
            importer = MBAImporter(self.filepath)
            importer.read_header()
            animations = importer.read_animation_list()

            wanted = [f.strip().lower()
                      for f in self.animation_filter.split(",") if f.strip()]
            if wanted:
                animations = [a for a in animations
                              if any(w in a.name.lower() for w in wanted)]
            if self.max_animations:
                animations = animations[:self.max_animations]

            if not animations:
                self.report({'ERROR'}, "No animations matched the name filter")
                return {'CANCELLED'}

            scene = context.scene
            fps = ANIMATION_FPS
            base_name = os.path.splitext(os.path.basename(self.filepath))[0]

            if armature_obj.animation_data is None:
                armature_obj.animation_data_create()

            imported = []
            skipped = []
            worst_match = 1.0
            for animation in animations:
                nodes, parents = importer.read_animation_nodes(animation)
                action, slot, keyed = build_animation_action(
                    armature_obj, nodes, parents, animation, rest_frames, fps,
                    f"{base_name}_{animation.name}")
                if keyed == 0:
                    skipped.append(animation.name)
                    bpy.data.actions.remove(action)
                    continue
                if nodes:
                    worst_match = min(worst_match, keyed / len(nodes))
                imported.append((animation, action, slot,
                                 _animation_closes(nodes, animation.length)))

            if not imported:
                self.report({'ERROR'},
                            "No animation node matched a bone of "
                            f"'{armature_obj.name}' - wrong skeleton for this pack?")
                return {'CANCELLED'}

            if self.match_scene_frame_rate:
                scene.render.fps = int(round(ANIMATION_FPS))
                scene.render.fps_base = 1.0

            if self.push_to_nla:
                for animation, action, slot, _closes in imported:
                    track = armature_obj.animation_data.nla_tracks.new()
                    track.name = action.name
                    strip = track.strips.new(action.name, 1, action)
                    strip.action_slot = slot
                    track.mute = True

            if self.assign_first:
                animation, action, slot, closes = imported[0]
                armature_obj.animation_data.action = action
                armature_obj.animation_data.action_slot = slot
                scene.frame_start = 1
                last = int(round(1.0 + animation.length * fps))
                # The closing key repeats the opening pose, so playing up to it
                # would show that pose twice every time round.
                scene.frame_end = max(2, last - 1 if closes else last)
                scene.frame_set(scene.frame_start)

            message = f"Imported {len(imported)} animations from {base_name}"
            if skipped:
                message += f" ({len(skipped)} matched no bones)"
            # Bones are matched by name, so a pack built for another skeleton can
            # still drive part of this one. Say so rather than looking clean.
            level = 'INFO'
            if worst_match < 0.9:
                message += f", only {worst_match * 100:.0f}% of nodes matched a bone"
                level = 'WARNING'
            self.report({level}, message)
            logger.log(message, force=True)
            return {'FINISHED'}

        except Exception as e:
            logger.error(f"MBA import failed: {e}")
            traceback.print_exc()
            self.report({'ERROR'}, f"Failed to import animation: {e}")
            return {'CANCELLED'}
        finally:
            if importer:
                importer.close()


def menu_func_import(self, context):
    self.layout.operator(IMPORT_MDB_OT_operator.bl_idname, text="Witcher MDB (.mdb)")

def menu_func_import_mba(self, context):
    self.layout.operator(IMPORT_MBA_OT_operator.bl_idname, text="Witcher Animation (.mba)")

def register():
    bpy.utils.register_class(IMPORT_MDB_OT_operator)
    bpy.utils.register_class(IMPORT_MBA_OT_operator)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import_mba)

def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import_mba)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    bpy.utils.unregister_class(IMPORT_MBA_OT_operator)
    bpy.utils.unregister_class(IMPORT_MDB_OT_operator)

if __name__ == "__main__":
    register()