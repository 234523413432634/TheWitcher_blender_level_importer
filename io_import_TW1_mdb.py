bl_info = {
    "name": "The Witcher 1 MDB Importer",
    "author": "Angry Catster",
    "version": (1, 0, 0),
    "blender": (5, 0, 1),
    "location": "File > Import > Witcher MDB (.mdb)",
    "description": "Import The Witcher 1 .mdb model files",
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
from mathutils import Vector, Matrix, Quaternion
from collections import defaultdict
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, BoolProperty, FloatProperty, EnumProperty, IntProperty
from bpy.types import Operator

# Node Types
NODE_TYPE_NODE = 0x00000001
NODE_TYPE_LIGHT = 0x00000003
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

# File versions
FILE_VERSION_133 = 133
FILE_VERSION_136 = 136

# Arbitrary scale multiplier for tree meshes
TREE_SCALE_MULTIPLIER = 32.0

DEFAULT_MATERIAL_ALPHA = 0.2

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
    "flarka", "sun_dummy", "cien", "blendbox", "woda_walkmesh", "Wm_woda"
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
    
    def is_transparent(self):
        transparent_shaders = [
            "dblsided_atest", "leaves", "leaves_lm", 
            "leaves_lm_bill", "leaves_singles", "transparency_2ps"
        ]
        return self.shader in transparent_shaders

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
            logger.log(f"  Mapping texture '{tex_name}' -> '{mapped_name}'")
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
            if line.startswith("shader "):
                continue
                
            s = -1
            n = 0
            
            if line.startswith("texture texture0 "):
                s = 17
                n = 0 if not has_shader_tex else 1
                logger.log(f"  Found texture0 at index {n}")
            elif line.startswith("texture texture1 "):
                s = 17
                n = 1 if not has_shader_tex else 2
                logger.log(f"  Found texture1 at index {n}")
            elif line.startswith("texture texture2 "):
                s = 17
                n = 2 if not has_shader_tex else 3
                logger.log(f"  Found texture2 at index {n}")
            elif line.startswith("texture texture3 "):
                s = 17
                n = 3 if not has_shader_tex else 4
                logger.log(f"  Found texture3 at index {n}")
            elif line.startswith("texture tex "):
                s = 12
                n = 0 if not has_shader_tex else 1
                logger.log(f"  Found tex at index {n}")
            elif line.startswith("texture texture_layer0 "):
                s = 23
                n = 0 if not has_shader_tex else 1
                logger.log(f"  Found texture_layer0 at index {n}")
                
            if s != -1:
                tex_name = line[s:].strip()
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

        light_obj = bpy.data.objects.new(name=node_name, object_data=light_data_bl)
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
        normals = []
        if normals_def.nb_used_entries > 0:
            seek_pos = self.model_data.offset_raw_data + normals_def.first_elem_offset
            self.reader.seek(seek_pos)
            for i in range(normals_def.nb_used_entries):
                x = self.reader.read_f32()
                y = self.reader.read_f32()
                z = self.reader.read_f32()
                n = controllers.global_transform.to_3x3() @ Vector((x, y, z))
                n.normalize()
                normals.append(n)
        
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
        if len(texture_strings) > 0 and texture_strings[0] == "_shader_" and len(texture_strings) > 1 and texture_strings[1]:
            material_file_uv_index = 1
            logger.log(f"  Material file reference: {texture_strings[1]} (UV index {material_file_uv_index})")
            mat_parser = self.load_material_file(texture_strings[1])
            if mat_parser and mat_parser.has_material():
                diffuse = mat_parser.get_diffuse_texture()
                if diffuse:
                    textures_to_use.append(diffuse)
                    texture_uv_indices.append(material_file_uv_index)
                    logger.log(f"  Diffuse from material file: {diffuse} (using UV{material_file_uv_index})")
        
        # If no material file, try embedded textures for diffuse
        if not textures_to_use:
            for i, tex in enumerate(embedded_textures):
                if tex and i < len(t_verts_defs) and t_verts_defs[i].nb_used_entries > 0:
                    mapped_tex = self.map_texture_name(tex)
                    if mapped_tex != tex:
                        tex = mapped_tex
                    
                    if lightmap_texture and (tex == light_map_name or tex == lightmap_texture):
                        continue
                    textures_to_use.append(tex)
                    texture_uv_indices.append(i)
                    logger.log(f"  Diffuse from embedded[{i}]: {tex}")
        
        # Then try static textures from node for diffuse
        if not textures_to_use:
            for i, tex in enumerate(texture_strings):
                if tex and tex != "NULL" and i < len(t_verts_defs) and t_verts_defs[i].nb_used_entries > 0:
                    mapped_tex = self.map_texture_name(tex)
                    if mapped_tex != tex:
                        tex = mapped_tex
                    
                    if lightmap_texture and (tex == light_map_name or tex == lightmap_texture):
                        continue
                    if tex not in textures_to_use:
                        textures_to_use.append(tex)
                        texture_uv_indices.append(i)
                        logger.log(f"  Diffuse from static[{i}]: {tex}")
        
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
                logger.log(f"  Texture not found, skipping: {tex}")
        
        # If we still have no textures but have a lightmap, use just the lightmap
        if not valid_textures and lightmap_texture and self.find_texture_file(lightmap_texture):
            valid_textures = [lightmap_texture]
            valid_indices = [lightmap_uv_index]
            logger.log(f"  Using only lightmap: {lightmap_texture}")
        
        logger.log(f"  Final textures: {valid_textures}")
        logger.log(f"  UV indices: {valid_indices}")
        
        uv_sets = []
        for uv_idx in range(4):
            if uv_idx < len(t_verts_defs) and t_verts_defs[uv_idx].nb_used_entries > 0:
                uv_def = t_verts_defs[uv_idx]
                seek_pos = self.model_data.offset_raw_data + uv_def.first_elem_offset
                self.reader.seek(seek_pos)
                uvs = []
                for j in range(uv_def.nb_used_entries):
                    u = self.reader.read_f32()
                    v = self.reader.read_f32()
                    uvs.append((u, 1.0 - v))
                uv_sets.append(uvs)
        
        for i in range(len(uv_sets)):
            while len(uv_sets[i]) < vertex_def.nb_used_entries:
                uv_sets[i].append((0.0, 0.0))
        
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
            'alpha': controllers.alpha if controllers.alpha < 1.0 else None,
            'is_transparent': is_transparent,
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
        
        normals = []
        if normals_def.nb_used_entries > 0:
            seek_pos = self.model_data.offset_raw_data + normals_def.first_elem_offset
            self.reader.seek(seek_pos)
            for i in range(normals_def.nb_used_entries):
                x = self.reader.read_f32()
                y = self.reader.read_f32()
                z = self.reader.read_f32()
                n = controllers.global_transform.to_3x3() @ Vector((x, y, z))
                n.normalize()
                normals.append(n)
        
        base_uvs = []
        uv_def = t_verts_defs[0] if len(t_verts_defs) > 0 else None
        if uv_def and uv_def.nb_used_entries > 0:
            seek_pos = self.model_data.offset_raw_data + uv_def.first_elem_offset
            self.reader.seek(seek_pos)
            for i in range(uv_def.nb_used_entries):
                u = self.reader.read_f32()
                v = self.reader.read_f32()
                base_uvs.append((u, 1.0 - v))
        
        while len(base_uvs) < len(vertices):
            base_uvs.append((0.0, 0.0))
        
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
            
            if has_texture and texture_name and weights_def.nb_used_entries > 0:
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
            'lightmap_uvs': base_uvs,
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
        
        self.reader.seek(72 + 16 + 60, 1)
        
        texture_strings = []
        for i in range(4):
            tex = self.reader.read_string(64)
            if tex == "NULL":
                tex = ""
            texture_strings.append(tex)
            if tex:
                logger.log(f"  Skin texture {i}: {tex}")
        
        self.reader.seek(61, 1)
        
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
        
        normals = []
        if normals_def.nb_used_entries > 0:
            seek_pos = self.model_data.offset_raw_data + normals_def.first_elem_offset
            self.reader.seek(seek_pos)
            for i in range(normals_def.nb_used_entries):
                x = self.reader.read_f32()
                y = self.reader.read_f32()
                z = self.reader.read_f32()
                n = controllers.global_transform.to_3x3() @ Vector((x, y, z))
                n.normalize()
                normals.append(n)
        
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
        else:
            # Process embedded textures
            for i, tex in enumerate(embedded_textures):
                if not tex:
                    continue
                
                uv_index = i + 1 if shader_consumes_slot else i
                
                # Check if this UV set actually has data
                if uv_index < len(t_verts_defs) and t_verts_defs[uv_index].nb_used_entries > 0:
                    if tex == light_map_name and self.time_of_day != 'NONE':
                        tex = self.evaluate_time_of_day_texture(tex, True)
                        if tex:
                            textures_to_use.append(tex)
                            texture_uv_indices.append(uv_index)
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
                
                uv_index = i + 1 if shader_consumes_slot else i
                
                if uv_index < len(t_verts_defs) and t_verts_defs[uv_index].nb_used_entries > 0:
                    if tex == light_map_name and self.time_of_day != 'NONE':
                        tex = self.evaluate_time_of_day_texture(tex, True)
                        if tex and tex not in textures_to_use:
                            textures_to_use.append(tex)
                            texture_uv_indices.append(uv_index)
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
        
        # Read UV sets
        uv_sets = []
        for uv_idx in range(4):
            if uv_idx < len(t_verts_defs) and t_verts_defs[uv_idx].nb_used_entries > 0:
                uv_def = t_verts_defs[uv_idx]
                seek_pos = self.model_data.offset_raw_data + uv_def.first_elem_offset
                self.reader.seek(seek_pos)
                uvs = []
                for i in range(uv_def.nb_used_entries):
                    u = self.reader.read_f32()
                    v = self.reader.read_f32()
                    uvs.append((u, 1.0 - v))
                uv_sets.append(uvs)
        
        # Ensure all UV sets have correct length
        for i in range(len(uv_sets)):
            while len(uv_sets[i]) < vertex_def.nb_used_entries:
                uv_sets[i].append((0.0, 0.0))
        
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
            'alpha': controllers.alpha if controllers.alpha < 1.0 else None,
            'is_transparent': controllers.alpha < 1.0,
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
                
                if node_data and node_type != NODE_TYPE_SPEEDTREE and node_type != NODE_TYPE_LIGHT:
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
        
        logger.log(f"\n{'='*60}", force=True)
        logger.log(f"Blender Witcher MDB Importer", force=True)
        logger.log(f"{'='*60}", force=True)
        
        importer = None
        
        try:
            importer = MDBImporter(
                self.filepath, 
                self.game_path, 
                self.time_of_day,
                self.import_speedtrees,
                self.import_skeletons,
                self.debug_mode
            )
            node_data_list = importer.import_model()
            
            if not node_data_list and (not self.import_speedtrees or not importer.speedtree_instances):
                self.report({'ERROR'}, "No meshes, lights, or trees found in file")
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
                
                # Create lights
                for light_data in light_data_list:
                    light_obj = importer.create_light_object(light_data, collection)
                    if light_obj:
                        light_count += 1
            
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
            self.report({'INFO'}, f"Imported {imported_count} meshes, {light_count} lights, {tree_instance_count} trees, {len(importer.bone_list)} bones")
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
        mat.use_backface_culling = True
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
                
                valid_layers = [(i, layer) for i, layer in enumerate(mesh_data['layers']) 
                               if layer.get('texture') and layer.get('weights') and 
                               len(layer['weights']) == len(vertices) and
                               importer.find_texture_file(layer['texture'])]
                
                if valid_layers:
                    for batch_idx in range(0, len(valid_layers), 3):
                        batch_layers = valid_layers[batch_idx:batch_idx + 3]
                        
                        vcol_layer = mesh.vertex_colors.new(name=f"Weights_{batch_idx // 3}")
                        
                        if vcol_layer:
                            for loop in mesh.loops:
                                vert_idx = loop.vertex_index
                                rgb = [0.0, 0.0, 0.0, 1.0]
                                
                                for channel_idx, (orig_idx, layer) in enumerate(batch_layers):
                                    if channel_idx < 3 and vert_idx < len(layer['weights']):
                                        rgb[channel_idx] = layer['weights'][vert_idx]
                                
                                vcol_layer.data[loop.index].color = tuple(rgb)
                            
                            logger.log(f"    Created packed vertex color layer Weights_{batch_idx // 3} with {len(batch_layers)} layers")
            
            elif 'uv_sets' in mesh_data and mesh_data['uv_sets']:
                for uv_idx, uv_set in enumerate(mesh_data['uv_sets']):
                    if uv_idx >= 4:
                        break
                    
                    if len(uv_set) == len(vertices):
                        uv_name = f"UVMap"
                        if uv_idx > 0:
                            uv_name = f"UVMap.{uv_idx}"
                        
                        uv_layer = mesh.uv_layers.new(name=uv_name)
                        if uv_layer:
                            for i, loop in enumerate(mesh.loops):
                                if loop.vertex_index < len(uv_set):
                                    uv_layer.data[i].uv = uv_set[loop.vertex_index]
            
            if mesh_data.get('normals') and len(mesh_data['normals']) == len(vertices):
                try:
                    mesh.use_auto_smooth = True
                    mesh.normals_split_custom_set_from_vertices(mesh_data['normals'])
                except:
                    pass
            
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
            
            for poly in obj.data.polygons:
                poly.use_smooth = True
            
            logger.log(f"  Created object: {obj_name} with {len(vertices)} verts, {len(polygons)} faces")
            return obj
        
        except Exception as e:
            logger.error(f"  Failed to create mesh object: {e}")
            traceback.print_exc()
            return None

    def _get_or_create_material(self, textures, uv_indices, mesh_data, importer):
        # Check if this is a water material first
        shader_type = mesh_data.get('shader_type', '')
        water_params = mesh_data.get('water_params')
        
        if importer.is_water_shader(shader_type) and water_params is not None:
            mat_name = "WaterMaterial"
            if water_params:
                if 'water_color' in water_params:
                    mat_name += f"_{water_params['water_color'][0]:.2f}"
            
            if mat_name in bpy.data.materials:
                logger.log(f"  Using existing water material: {mat_name}")
                mat = bpy.data.materials[mat_name]
                mat.use_backface_culling = True
                return mat
            
            logger.log(f"  Creating new water material: {mat_name}")
            return self._create_water_material(mesh_data, importer, mat_name)
        
        if not textures:
            mat_name = "DefaultMaterial"
        else:
            uv_parts = []
            for i, (tex, uv_idx) in enumerate(zip(textures, uv_indices)):
                tex_base = os.path.basename(tex) if tex else "none"
                uv_parts.append(f"{tex_base}_UV{uv_idx}")
            mat_name = "_".join(uv_parts)
        
        if mat_name in bpy.data.materials:
            logger.log(f"  Using existing material: {mat_name}")
            mat = bpy.data.materials[mat_name]
            mat.use_backface_culling = True
            return mat
        
        logger.log(f"  Creating new material: {mat_name}")
        return self._create_material(textures, uv_indices, mesh_data, importer, mat_name)

    def _get_or_create_texture_paint_material(self, layers, lightmap_texture, mesh_data, importer):
        valid_layers = [(i, layer) for i, layer in enumerate(layers) 
                       if layer.get('texture') and importer.find_texture_file(layer['texture'])]
        
        if not valid_layers:
            return None
        
        mat_name = "TexturePaint"
        for _, layer in valid_layers:
            mat_name += f"_{os.path.basename(layer['texture'])}"
        
        if lightmap_texture and self.time_of_day != 'NONE':
            lightmap_name = os.path.basename(lightmap_texture)
            mat_name += f"_LM_{lightmap_name}_{self.time_of_day}"
        
        if mat_name in bpy.data.materials:
            logger.log(f"  Using existing texture paint material: {mat_name}")
            mat = bpy.data.materials[mat_name]
            mat.use_backface_culling = True
            return mat
        
        logger.log(f"  Creating new texture paint material: {mat_name}")
        return self._create_texture_paint_material_packed(layers, lightmap_texture, mesh_data, importer, mat_name)

    def _create_material(self, textures, uv_indices, mesh_data, importer, mat_name=None):
        if not mat_name:
            mat_name = "DefaultMaterial"
        
        mat = bpy.data.materials.new(name=mat_name)
        mat.specular_intensity = 0.0
        mat.use_backface_culling = True
        
        mat.node_tree.nodes.clear()
        
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        
        output = nodes.new('ShaderNodeOutputMaterial')
        output.location = (800, 0)
        
        bsdf = nodes.new('ShaderNodeBsdfPrincipled')
        bsdf.location = (600, 0)
        bsdf.inputs['Specular IOR Level'].default_value = 0.0
        
        links.new(bsdf.outputs['BSDF'], output.inputs['Surface'])
        
        alpha_value = mesh_data.get('alpha')
        if alpha_value is None:
            alpha_value = 1.0
        else:
            try:
                alpha_value = float(alpha_value)
            except (TypeError, ValueError):
                alpha_value = 1.0
        
        # Determine if this material should skip alpha connection
        # These shaders use alpha for illumination/specular/other things
        skip_alpha_shaders = ["selfilum_b", "reflection_b", "specular", "skin_n_rim_ao_mh", "skin_n_rim_ao"]
        skip_alpha = False

        shader_type = mesh_data.get('shader_type', '')
        if shader_type in skip_alpha_shaders:
            skip_alpha = True
            logger.log(f"  Material uses shader '{shader_type}' - skipping alpha connection")
        
        if mesh_data.get('is_transparent', False) or (alpha_value < 1.0 and not skip_alpha):
            mat.blend_method = 'BLEND'
            bsdf.inputs['Alpha'].default_value = alpha_value
        
        if not textures:
            mat.blend_method = 'BLEND'
            bsdf.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
            bsdf.inputs['Alpha'].default_value = DEFAULT_MATERIAL_ALPHA
            return mat
        
        uv_nodes = {}
        for uv_idx in set(uv_indices):
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
        
        # Look for normal map
        normal_map_texture = None
        normal_map_uv_idx = 0
        
        for idx, tex_name in enumerate(textures):
            if not tex_name:
                continue
            
            base_name = tex_name
            if base_name.endswith('_n'):
                normal_map_texture = base_name
                normal_map_uv_idx = uv_indices[idx] if idx < len(uv_indices) else 0
                logger.log(f"  Found normal map from texture list: {normal_map_texture}")
                break
            
            if '_n' not in base_name:
                normal_candidate = base_name + '_n'
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
                
                # For special shaders, set alpha mode to NONE
                if skip_alpha:
                    img.alpha_mode = 'NONE'
                    logger.log(f"  Set alpha mode to NONE for {tex_name}")
                
                if uv_idx in uv_nodes:
                    links.new(uv_nodes[uv_idx].outputs['UV'], tex_node.inputs['Vector'])
                
                texture_nodes.append((idx, tex_node, uv_idx))

                if len(texture_nodes) == 1 and len(textures) > 1:
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
                
                if normal_map_uv_idx in uv_nodes:
                    normal_uv_node = uv_nodes[normal_map_uv_idx]
                else:
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
                
                if not skip_alpha and diffuse_node.image and diffuse_node.image.channels == 4:
                    links.new(diffuse_node.outputs['Alpha'], bsdf.inputs['Alpha'])
            else:
                links.new(lightmap_node.outputs['Color'], bsdf.inputs['Base Color'])
                if not skip_alpha and lightmap_node.image and lightmap_node.image.channels == 4:
                    links.new(lightmap_node.outputs['Alpha'], bsdf.inputs['Alpha'])
        else:
            # No lightmap - simple diffuse connection
            logger.log(f"  No lightmap found, using simple diffuse connection")
            if diffuse_node:
                links.new(diffuse_node.outputs['Color'], bsdf.inputs['Base Color'])
                
                if not skip_alpha and diffuse_node.image and diffuse_node.image.channels == 4:
                    links.new(diffuse_node.outputs['Alpha'], bsdf.inputs['Alpha'])
            elif texture_nodes:
                # Use the first available texture
                tex_node = texture_nodes[0][1]
                links.new(tex_node.outputs['Color'], bsdf.inputs['Base Color'])
                
                if not skip_alpha and tex_node.image and tex_node.image.channels == 4:
                    links.new(tex_node.outputs['Alpha'], bsdf.inputs['Alpha'])
        
        return mat

    def _create_texture_paint_material_packed(self, layers, lightmap_texture, mesh_data, importer, mat_name=None):
        valid_layers = [(i, layer) for i, layer in enumerate(layers) 
                       if layer.get('texture') and importer.find_texture_file(layer['texture'])]
        
        if not valid_layers:
            return None
        
        if not mat_name:
            mat_name = "TexturePaint"
            for _, layer in valid_layers:
                mat_name += f"_{os.path.basename(layer['texture'])}"
            if lightmap_texture and self.time_of_day != 'NONE':
                mat_name += f"_lm_{self.time_of_day}"
        
        if mat_name in bpy.data.materials:
            mat = bpy.data.materials[mat_name]
            mat.use_backface_culling = True
            return mat
        
        mat = bpy.data.materials.new(name=mat_name)
        mat.specular_intensity = 0.0
        mat.use_backface_culling = True
        
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
                alpha_value = float(alpha_value)
            except (TypeError, ValueError):
                alpha_value = 1.0
        
        if mesh_data.get('is_transparent', False) or alpha_value < 1.0:
            mat.blend_method = 'BLEND'
            bsdf.inputs['Alpha'].default_value = alpha_value
        
        uv_base = nodes.new('ShaderNodeUVMap')
        uv_base.location = (-2000, 400)
        uv_base.uv_map = "UVMap"
        uv_base.label = "Base UVs"
        
        mapping = nodes.new('ShaderNodeMapping')
        mapping.location = (-1700, 400)
        mapping.vector_type = 'POINT'
        mapping.inputs['Scale'].default_value = (50.0, 50.0, 50.0)
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
        
        current_output = None
        current_x = -1000
        
        for idx, (tex_node, weight_source, orig_idx) in enumerate(texture_weight_map):
            if idx == 0:
                current_output = tex_node.outputs['Color']
                continue
            
            mix_node = nodes.new('ShaderNodeMixRGB')
            mix_node.location = (current_x, 0)
            mix_node.blend_type = 'MIX'
            mix_node.label = f"Mix Layer {orig_idx}"
            
            if current_output:
                links.new(current_output, mix_node.inputs['Color1'])
            else:
                mix_node.inputs['Color1'].default_value = (0.0, 0.0, 0.0, 1.0)
            
            links.new(tex_node.outputs['Color'], mix_node.inputs['Color2'])
            links.new(weight_source, mix_node.inputs['Fac'])
            
            current_output = mix_node.outputs['Color']
            current_x += 300
        
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

def menu_func_import(self, context):
    self.layout.operator(IMPORT_MDB_OT_operator.bl_idname, text="Witcher MDB (.mdb)")

def register():
    bpy.utils.register_class(IMPORT_MDB_OT_operator)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)

def unregister():
    bpy.utils.unregister_class(IMPORT_MDB_OT_operator)
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)

if __name__ == "__main__":
    register()