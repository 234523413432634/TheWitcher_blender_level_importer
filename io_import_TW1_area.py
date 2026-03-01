bl_info = {
    "name": "The Witcher 1 MOD Importer",
    "author": "Angry Catster",
    "version": (1, 0, 0),
    "blender": (5, 0, 1),
    "location": "File > Import > Witcher MOD (.mod)",
    "description": "Import The Witcher 1 .mod/.adv files",
    "category": "Import-Export",
}

import bpy
import struct
import os
import math
from mathutils import Vector, Quaternion, Matrix
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty, BoolProperty, EnumProperty, IntProperty, CollectionProperty
from bpy.types import Operator, OperatorFileListElement, PropertyGroup, UIList, AddonPreferences

# Field types from GFF3
kFieldTypeByte = 0
kFieldTypeChar = 1
kFieldTypeUint16 = 2
kFieldTypeSint16 = 3
kFieldTypeUint32 = 4
kFieldTypeSint32 = 5
kFieldTypeUint64 = 6
kFieldTypeSint64 = 7
kFieldTypeFloat = 8
kFieldTypeDouble = 9
kFieldTypeExoString = 10
kFieldTypeResRef = 11
kFieldTypeLocString = 12
kFieldTypeVoid = 13
kFieldTypeStruct = 14
kFieldTypeList = 15
kFieldTypeOrientation = 16
kFieldTypeVector = 17
kFieldTypeStrRef = 18

# File type mappings
kFileTypeIFO = 2014  # Module information
kFileTypeARE = 2012  # Static area data
kFileTypeGIT = 2023  # Dynamic area data
kFileTypeUTC = 2027  # Creature template
kFileTypeUTD = 2042  # Door template
kFileTypeUTP = 2044  # Placeable template
kFileTypeUTW = 2058  # Waypoint template
kFileTypeDLG = 2029  # Dialog

LANGUAGE_NAMES = {
    6: "English",
    22: "French",
    20: "German",
    26: "Italian",
    24: "Spanish",
    10: "Polish",
    30: "Czech",
    32: "Hungarian",
    28: "Russian",
    40: "Korean",
    42: "Traditional Chinese",
    44: "Simplified Chinese"
}

# Language options for UI dropdown
LANGUAGE_ITEMS = [
    ('6', "English", "English"),
    ('22', "French", "Français"),
    ('20', "German", "Deutsch"),
    ('26', "Italian", "Italiano"),
    ('24', "Spanish", "Español"),
    ('10', "Polish", "Polski"),
    ('30', "Czech", "Čeština"),
    ('32', "Hungarian", "Magyar"),
    ('28', "Russian", "Русский"),
    ('40', "Korean", "한국어"),
    ('42', "Traditional Chinese", "繁體中文"),
    ('44', "Simplified Chinese", "简体中文"),
]

# Default language priority
DEFAULT_LANGUAGE = '6'  # English

MDB_IMPORTER_AVAILABLE = hasattr(bpy.ops.import_scene, 'mdb')
if MDB_IMPORTER_AVAILABLE:
    print("MDB importer operator found: bpy.ops.import_scene.mdb")
    
    def load_mdb_model(mdb_path, game_root, time_of_day, import_speedtrees=False):
        """Load MDB using the registered operator"""
        try:
            old_selection = set(bpy.context.selected_objects)
            old_active = bpy.context.active_object
            
            # Store current view layer objects to detect new ones
            old_objects = set(bpy.context.view_layer.objects)
            
            result = bpy.ops.import_scene.mdb(
                filepath=mdb_path,
                game_path=game_root,
                time_of_day=time_of_day,
                import_speedtrees=import_speedtrees,
                debug_mode=False
            )
            
            new_objects = set(bpy.context.view_layer.objects) - old_objects
            
            if new_objects:
                print(f"    Imported {len(new_objects)} objects from {os.path.basename(mdb_path)}")
                return list(new_objects)
            else:
                print(f"    No objects imported from {os.path.basename(mdb_path)}")
                return None
                
        except Exception as e:
            print(f"    Failed to load MDB via operator: {e}")
            import traceback
            traceback.print_exc()
            return None
else:
    print("WARNING: MDB importer operator not found. Will use placeholder cubes.")
    
    def load_mdb_model(mdb_path, game_root, time_of_day, import_speedtrees=False):
        """Fallback function when importer not available"""
        return None


# ============
# Addon Preferences
# ============

class WitcherImporterPreferences(AddonPreferences):
    bl_idname = __name__

    game_path: StringProperty(
        name="Game Root Path",
        description="Path to The Witcher 1 installation root (for MDB files)",
        subtype='DIR_PATH',
        default="D:\\Path\\to\\unpacked\\files",
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "game_path")


# ============
# UI List and Area Info Classes
# ============

class AREA_UL_areas(UIList):
    """UI List for displaying areas"""
    
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            selected_lang = '6'
            if hasattr(active_data, 'current_language'):
                selected_lang = str(active_data.current_language)
            elif hasattr(context, 'active_operator') and hasattr(context.active_operator, 'current_language'):
                selected_lang = str(context.active_operator.current_language)
                
            main_row = layout.row(align=True)
            
            # Area code (e.g., "l17")
            main_row.label(text=item.name)
            
            # Get localized name for selected language
            if item.localized_names and selected_lang in item.localized_names:
                localized = item.localized_names[selected_lang]
                if localized:
                    main_row.label(text=f"({localized})")
                    return
            
            # Fallback to best name if specific language not available
            if item.localized_name:
                main_row.label(text=f"({item.localized_name})")
            else:
                main_row.label(text="(no name)")
                
        elif self.layout_type in {'GRID'}:
            layout.alignment = 'CENTER'
            layout.label(text=item.name)


class AreaInfo(PropertyGroup):
    """Property group for area information"""
    name: StringProperty(name="Area Code", description="Area code (e.g., l17, l09)")
    localized_name: StringProperty(name="Localized Name", description="Best available localized name")
    has_are: BoolProperty(name="Has ARE File", default=False)
    has_git: BoolProperty(name="Has GIT File", default=False)
    tileset: StringProperty(name="Tileset", default="")
    resource_count: IntProperty(name="Resource Count", default=0)
    index: IntProperty(name="Index")
    name_count: IntProperty(name="Number of Languages", default=0, description="Number of language strings found")
    available_languages: StringProperty(name="Available Languages", default="", description="List of available languages")
    localized_names_string: StringProperty(name="Localized Names", default="")
    
    @property
    def localized_names(self):
        """Parse the localized names string into a dictionary"""
        result = {}
        if self.localized_names_string:
            for entry in self.localized_names_string.split('|'):
                if ':' in entry:
                    lang_id, text = entry.split(':', 1)
                    result[lang_id] = text
        return result
    
    @localized_names.setter
    def localized_names(self, value):
        """Store dictionary as string"""
        if isinstance(value, dict):
            entries = []
            for lang_id, text in value.items():
                if text:
                    entries.append(f"{lang_id}:{text}")
            self.localized_names_string = '|'.join(entries)
        else:
            self.localized_names_string = ""


# ============
# ERF File Parser
# ============

class ERFFile:
    """Parser for ERF archive files"""
    def __init__(self, filepath):
        self.filepath = filepath
        self.file = None
        self.resources = []
        self.header = {}
        self.description = ""
        self.areas = []
        
    def read_uint32(self):
        data = self.file.read(4)
        if len(data) < 4:
            raise struct.error("Insufficient data for uint32")
        return struct.unpack('<I', data)[0]
    
    def read_uint16(self):
        data = self.file.read(2)
        if len(data) < 2:
            raise struct.error("Insufficient data for uint16")
        return struct.unpack('<H', data)[0]
    
    def read_string(self, length, encoding='ascii'):
        data = self.file.read(length)
        if b'\x00' in data:
            data = data.split(b'\x00')[0]
        return data.decode(encoding, errors='ignore')
    
    def parse(self):
        print(f"\n{'='*60}")
        print(f"Parsing ERF file: {os.path.basename(self.filepath)}")
        print(f"{'='*60}")
        
        self.file = open(self.filepath, 'rb')
        
        try:
            file_type = self.read_uint32()
            version = self.read_uint32()
            
            file_type_str = struct.pack('<I', file_type).decode('ascii', errors='ignore')[::-1]
            version_str = struct.pack('<I', version).decode('ascii', errors='ignore')[::-1]
            
            self.header['file_type'] = file_type_str
            self.header['version'] = version_str
            
            print(f"File Type: {file_type_str} (0x{file_type:08X})")
            print(f"Version: {version_str} (0x{version:08X})")
            
            self._parse_v10()
                
        except Exception as e:
            print(f"Error parsing ERF: {e}")
        finally:
            self.file.close()
            
        return self.header, self.resources, self.description
    
    def _parse_v10(self):
        # V1.0 header format
        try:
            lang_count = self.read_uint32()
            desc_size = self.read_uint32()
            res_count = self.read_uint32()
            off_description = self.read_uint32()
            off_key_list = self.read_uint32()
            off_res_list = self.read_uint32()
            build_year = self.read_uint32() + 1900
            build_day = self.read_uint32()
            description_id = self.read_uint32()
            
            self.file.seek(self.file.tell() + 116)
            
            self.header.update({
                'language_count': lang_count,
                'description_size': desc_size,
                'resource_count': res_count,
                'description_offset': off_description,
                'key_list_offset': off_key_list,
                'res_list_offset': off_res_list,
                'build_year': build_year,
                'build_day': build_day,
                'description_id': description_id
            })
            
            print(f"\nResources: {res_count}")
            print(f"Built: Year {build_year}, Day {build_day}")
            
            # Read description if present
            if off_description != 0xFFFFFFFF and off_description > 0 and lang_count > 0:
                current_pos = self.file.tell()
                try:
                    self.file.seek(off_description)
                    
                    for i in range(lang_count):
                        lang_id = self.read_uint32()
                        string_len = self.read_uint32()
                        string_data = self.read_string(string_len)
                        print(f"  Description ({lang_id}): {string_data}")
                        self.description = string_data
                except:
                    print("  Error reading description")
                finally:
                    self.file.seek(current_pos)
            
            # Read key list (resource names and types)
            self.file.seek(off_key_list)
            resources = []
            
            area_names = set()
            
            for i in range(res_count):
                name = self.read_string(16).lower()
                res_id = self.read_uint32()
                res_type = self.read_uint16()
                self.file.seek(self.file.tell() + 2)
                
                resources.append({
                    'name': name,
                    'type': res_type,
                    'type_name': self._get_file_type_name(res_type),
                    'index': i,
                    'res_id': res_id
                })
                
                if res_type in [kFileTypeARE, kFileTypeGIT]:
                    area_names.add(name)
            
            # Read resource list
            self.file.seek(off_res_list)
            for i in range(res_count):
                offset = self.read_uint32()
                size = self.read_uint32()
                resources[i]['offset'] = offset
                resources[i]['size'] = size
            
            self.resources = resources
            
            # Build area list
            for area_name in sorted(area_names):
                area_info = {
                    'name': area_name,
                    'has_are': False,
                    'has_git': False,
                    'tileset': '',
                    'resource_count': 0,
                    'localized_names': {},
                    'best_name': ''
                }
                
                for res in resources:
                    if res['name'] == area_name:
                        area_info['resource_count'] += 1
                        if res['type'] == kFileTypeARE:
                            area_info['has_are'] = True
                            are_data = self._extract_are_info(res['index'])
                            if are_data:
                                if are_data.get('tileset'):
                                    area_info['tileset'] = are_data['tileset']
                                if are_data.get('localized_names'):
                                    area_info['localized_names'] = are_data['localized_names']
                        elif res['type'] == kFileTypeGIT:
                            area_info['has_git'] = True
                
                best_name = self._get_best_localized_name(area_info['localized_names'])
                area_info['best_name'] = best_name
                
                self.areas.append(area_info)
            
            # Print resource summary
            type_counts = {}
            for res in resources:
                type_name = res['type_name']
                type_counts[type_name] = type_counts.get(type_name, 0) + 1
            
            print("\nResource types found:")
            for type_name, count in sorted(type_counts.items()):
                print(f"  {type_name}: {count}")
            
            print(f"\nAreas detected: {len(self.areas)}")
            for area in self.areas:
                tileset_info = f" [Tileset: {area['tileset']}]" if area['tileset'] else ""
                name_info = f" '{area['best_name']}'" if area['best_name'] else ""
                lang_count = len(area['localized_names'])
                lang_info = f" [{lang_count} languages]" if lang_count > 0 else ""
                print(f"  {area['name']}{name_info}{tileset_info}{lang_info}")
                
        except Exception as e:
            print(f"Error parsing V1.0 header: {e}")
    
    def _get_best_localized_name(self, localized_names):
        """Get the best localized name based on default language priority"""
        if not localized_names:
            return ""
        
        priority = [6, 10, 20, 22, 24, 26, 28, 30, 32, 40, 42, 44]
        
        for lang_id in priority:
            if lang_id in localized_names and localized_names[lang_id]:
                return localized_names[lang_id]
        
        for name in localized_names.values():
            if name:
                return name
        
        return ""
    
    def _extract_are_info(self, are_index):
        """Extract tileset name and localized names from an ARE file"""
        try:
            are_data = self.extract_resource(are_index)
            if not are_data or len(are_data) < 48:
                return None
            
            result = {
                'tileset': '',
                'localized_names': {}
            }
            
            # Use GFF3 parser to extract fields
            gff = GFF3File(are_data, f"area_{are_index}.are")
            struct = gff.parse()
            
            if struct:
                fields = struct.get('fields', {})
                
                if 'Tileset' in fields:
                    tileset_field = fields['Tileset']
                    if tileset_field['type'] == kFieldTypeResRef:
                        tileset = gff.get_field_value(tileset_field)
                        if tileset:
                            result['tileset'] = tileset
                            print(f"      Extracted tileset: {tileset}")
                
                if 'Name' in fields:
                    name_field = fields['Name']
                    if name_field['type'] == kFieldTypeLocString:
                        print(f"      Found Name field of type LocString, parsing...")
                        locstring_data = self._parse_locstring(gff, name_field)
                        if locstring_data:
                            result['localized_names'] = locstring_data
                            print(f"      Extracted {len(locstring_data)} localized names")
                            for lang_id, text in locstring_data.items():
                                lang_name = LANGUAGE_NAMES.get(lang_id, f"Unknown({lang_id})")
                                if text:
                                    print(f"        Language {lang_id} ({lang_name}): {text[:50]}{'...' if len(text) > 50 else ''}")
            
            return result
            
        except Exception as e:
            print(f"    Error extracting ARE info: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _parse_locstring(self, gff, field):
        """Parse a LocString field and return dictionary of language_id -> text"""
        result = {}
        
        try:
            field_data_offset = gff.header.get('field_data_offset', 0)
            offset = field['data']
            
            saved_pos = gff.pos
            gff.pos = field_data_offset + offset
            
            total_size = gff.read_uint32()
            str_ref = gff.read_uint32()
            
            print(f"        LocString header: total_size={total_size}, str_ref=0x{str_ref:08X}")
            print(f"        Current position: 0x{gff.pos:X}")
            
            num_strings = gff.read_uint32()
            print(f"        Number of substrings: {num_strings}")

            for i in range(num_strings):
                try:
                    lang_id = gff.read_uint32()
                    str_len = gff.read_uint32()
                    
                    print(f"          Entry {i}: lang_id={lang_id}, str_len={str_len}, pos=0x{gff.pos:X}")
                    
                    if str_len > 0 and str_len < 65536:
                        text = gff.read_string(str_len, 'utf-8')
                        result[lang_id] = text
                        print(f"            Text: {text[:50]}{'...' if len(text) > 50 else ''}")
                    elif str_len == 0:
                        result[lang_id] = ""
                        print(f"            Empty string")
                    else:
                        print(f"            Invalid string length: {str_len}")
                        break
                        
                except Exception as e:
                    print(f"        Error reading string entry {i}: {e}")
                    break
            
            print(f"        Read {len(result)} language entries")
            
            gff.pos = saved_pos
            
        except Exception as e:
            print(f"        Error parsing LocString: {e}")
        
        return result
    
    def _get_file_type_name(self, type_id):
        """Convert file type ID to name"""
        type_map = {
            kFileTypeIFO: "IFO (Module Info)",
            kFileTypeARE: "ARE (Area Static)",
            kFileTypeGIT: "GIT (Area Dynamic)",
            kFileTypeUTC: "UTC (Creature)",
            kFileTypeUTD: "UTD (Door)",
            kFileTypeUTP: "UTP (Placeable)",
            kFileTypeUTW: "UTW (Waypoint)",
            kFileTypeDLG: "DLG (Dialog)",
            2017: "2DA (2D Array)",
            2018: "TLK (Talk Table)",
            2022: "TXI (Texture Info)",
            2033: "DDS (Texture)",
            2078: "OGG (Audio)",
        }
        return type_map.get(type_id, f"Unknown (0x{type_id:04X})")
    
    def extract_resource(self, index):
        """Extract a specific resource by index"""
        if index >= len(self.resources):
            return None
        
        res = self.resources[index]
        self.file = open(self.filepath, 'rb')
        try:
            self.file.seek(res['offset'])
            data = self.file.read(res['size'])
            return data
        finally:
            self.file.close()
    
    def get_area_resources(self, area_name):
        """Get all resources for a specific area"""
        area_resources = []
        for res in self.resources:
            if res['name'] == area_name.lower():
                area_resources.append(res)
        return area_resources


# ============
# GFF3 File Parser
# ============

class GFF3File:
    """Parser for GFF3 (Generic File Format v3) files"""
    
    def __init__(self, data, filename=""):
        self.data = data
        self.filename = filename
        self.pos = 0
        self.header = {}
        self.struct_offsets = []
        self.structs = []
        
    def read_uint32(self):
        if self.pos + 4 > len(self.data):
            raise struct.error(f"Insufficient data for uint32 at position 0x{self.pos:X}")
        val = struct.unpack('<I', self.data[self.pos:self.pos+4])[0]
        self.pos += 4
        return val
    
    def read_uint16(self):
        if self.pos + 2 > len(self.data):
            raise struct.error(f"Insufficient data for uint16 at position 0x{self.pos:X}")
        val = struct.unpack('<H', self.data[self.pos:self.pos+2])[0]
        self.pos += 2
        return val
    
    def read_sint16(self):
        if self.pos + 2 > len(self.data):
            raise struct.error(f"Insufficient data for sint16 at position 0x{self.pos:X}")
        val = struct.unpack('<h', self.data[self.pos:self.pos+2])[0]
        self.pos += 2
        return val
    
    def read_sint32(self):
        if self.pos + 4 > len(self.data):
            raise struct.error(f"Insufficient data for sint32 at position 0x{self.pos:X}")
        val = struct.unpack('<i', self.data[self.pos:self.pos+4])[0]
        self.pos += 4
        return val
    
    def read_byte(self):
        if self.pos + 1 > len(self.data):
            raise struct.error(f"Insufficient data for byte at position 0x{self.pos:X}")
        val = self.data[self.pos]
        self.pos += 1
        return val
    
    def read_float(self):
        if self.pos + 4 > len(self.data):
            raise struct.error(f"Insufficient data for float at position 0x{self.pos:X}")
        val = struct.unpack('<f', self.data[self.pos:self.pos+4])[0]
        self.pos += 4
        return val
    
    def read_double(self):
        if self.pos + 8 > len(self.data):
            raise struct.error(f"Insufficient data for double at position 0x{self.pos:X}")
        val = struct.unpack('<d', self.data[self.pos:self.pos+8])[0]
        self.pos += 8
        return val
    
    def read_string(self, length, encoding='ascii'):
        if self.pos + length > len(self.data):
            length = len(self.data) - self.pos
        data = self.data[self.pos:self.pos+length]
        self.pos += length
        if b'\x00' in data:
            data = data.split(b'\x00')[0]
        return data.decode(encoding, errors='ignore')
    
    def parse(self):
        print(f"\n  GFF3 File: {self.filename}")
        print(f"    Size: {len(self.data)} bytes")
        
        if len(self.data) < 48:
            print(f"    ERROR: File too small ({len(self.data)} bytes)")
            return None
        
        try:
            file_type = self.read_uint32()
            version = self.read_uint32()
            
            file_type_str = struct.pack('<I', file_type).decode('ascii', errors='ignore')[::-1]
            version_str = struct.pack('<I', version).decode('ascii', errors='ignore')[::-1]
            
            self.header['type'] = file_type_str
            self.header['version'] = version_str
            
            print(f"    Type: {file_type_str}")
            print(f"    Version: {version_str}")
            
            struct_offset = self.read_uint32()
            struct_count = self.read_uint32()
            field_offset = self.read_uint32()
            field_count = self.read_uint32()
            label_offset = self.read_uint32()
            label_count = self.read_uint32()
            field_data_offset = self.read_uint32()
            field_data_count = self.read_uint32()
            field_indices_offset = self.read_uint32()
            field_indices_count = self.read_uint32()
            list_indices_offset = self.read_uint32()
            list_indices_count = self.read_uint32()
            
            self.header.update({
                'struct_offset': struct_offset,
                'struct_count': struct_count,
                'field_offset': field_offset,
                'field_count': field_count,
                'label_offset': label_offset,
                'label_count': label_count,
                'field_data_offset': field_data_offset,
                'field_data_count': field_data_count,
                'field_indices_offset': field_indices_offset,
                'field_indices_count': field_indices_count,
                'list_indices_offset': list_indices_offset,
                'list_indices_count': list_indices_count,
            })
            
            print(f"    Structs: {struct_count}, Fields: {field_count}")
            print(f"    Field Data Offset: 0x{field_data_offset:X}, Count: {field_data_count}")
            
            self.struct_offsets = []
            for i in range(struct_count):
                self.struct_offsets.append(struct_offset + i * 12)
            
            for i, offset in enumerate(self.struct_offsets):
                self.pos = offset
                struct_data = self._parse_struct()
                self.structs.append(struct_data)

            if len(self.structs) > 0:
                return self.structs[0]
            else:
                print(f"    WARNING: No structs in file")
                return {'id': 0, 'fields': {}}
                
        except struct.error as e:
            print(f"    ERROR parsing GFF3: {e}")
            return None
        except Exception as e:
            print(f"    Unexpected error parsing GFF3: {e}")
            import traceback
            traceback.print_exc()
            return None
    
    def _parse_struct(self):
        """Parse a struct at current position"""
        if self.pos + 12 > len(self.data):
            print(f"    WARNING: Incomplete struct at position 0x{self.pos:X}")
            return {'id': 0, 'fields': {}}
        
        struct_id = self.read_uint32()
        field_or_index = self.read_uint32()
        field_count = self.read_uint32()
        
        struct_data = {
            'id': struct_id,
            'fields': {}
        }
        
        # Read fields
        if field_count == 1:
            # Single field
            field_pos = self.header.get('field_offset', 0) + field_or_index * 12
            if field_pos + 12 <= len(self.data):
                saved_pos = self.pos
                self.pos = field_pos
                field = self._parse_field()
                if field:
                    field_name = self._get_label_name(field['label_index'])
                    if field_name:
                        struct_data['fields'][field_name] = field
                self.pos = saved_pos
            
        elif field_count > 1:
            # Multiple fields
            indices_pos = self.header.get('field_indices_offset', 0) + field_or_index
            
            if indices_pos + field_count * 4 <= len(self.data):
                saved_pos = self.pos
                self.pos = indices_pos
                
                indices = []
                for i in range(field_count):
                    try:
                        indices.append(self.read_uint32())
                    except:
                        break
                
                for idx in indices:
                    field_pos = self.header.get('field_offset', 0) + idx * 12
                    if field_pos + 12 <= len(self.data):
                        self.pos = field_pos
                        field = self._parse_field()
                        if field:
                            field_name = self._get_label_name(field['label_index'])
                            if field_name:
                                struct_data['fields'][field_name] = field
                
                self.pos = saved_pos
        
        return struct_data
    
    def _parse_field(self):
        """Parse a field at current position"""
        if self.pos + 12 > len(self.data):
            return None
            
        field_type = self.read_uint32()
        label_index = self.read_uint32()
        data_or_offset = self.read_uint32()
        
        field = {
            'type': field_type,
            'type_name': self._get_field_type_name(field_type),
            'label_index': label_index,
            'data': data_or_offset
        }
        
        return field
    
    def _get_label_name(self, label_index):
        """Get label name from label index"""
        label_pos = self.header.get('label_offset', 0) + label_index * 16
        if label_pos + 16 > len(self.data):
            return f"label_{label_index}"
        
        saved_pos = self.pos
        self.pos = label_pos
        name = self.read_string(16)
        self.pos = saved_pos
        return name.strip()
    
    def _get_field_type_name(self, type_id):
        """Convert field type ID to name"""
        type_names = {
            kFieldTypeByte: "Byte",
            kFieldTypeChar: "Char",
            kFieldTypeUint16: "Uint16",
            kFieldTypeSint16: "Sint16",
            kFieldTypeUint32: "Uint32",
            kFieldTypeSint32: "Sint32",
            kFieldTypeUint64: "Uint64",
            kFieldTypeSint64: "Sint64",
            kFieldTypeFloat: "Float",
            kFieldTypeDouble: "Double",
            kFieldTypeExoString: "String",
            kFieldTypeResRef: "ResRef",
            kFieldTypeLocString: "LocString",
            kFieldTypeVoid: "Void",
            kFieldTypeStruct: "Struct",
            kFieldTypeList: "List",
            kFieldTypeOrientation: "Orientation",
            kFieldTypeVector: "Vector",
            kFieldTypeStrRef: "StrRef",
        }
        return type_names.get(type_id, f"Unknown({type_id})")
    
    def get_field_value(self, field):
        """Extract the actual value of a field based on its type"""
        field_type = field['type']
        data = field['data']
        
        try:
            if field_type == kFieldTypeByte:
                return data & 0xFF
                
            elif field_type == kFieldTypeChar:
                # Char is signed 8-bit
                val = data & 0xFF
                if val > 127:
                    val = val - 256
                return val
                
            elif field_type == kFieldTypeUint16:
                return data & 0xFFFF
                
            elif field_type == kFieldTypeSint16:
                # Sint16 is signed 16-bit
                val = data & 0xFFFF
                if val > 32767:
                    val = val - 65536
                return val
                
            elif field_type == kFieldTypeUint32:
                return data
                
            elif field_type == kFieldTypeSint32:
                # Sint32 is signed 32-bit
                if data > 2147483647:
                    data = data - 4294967296
                return data
                
            elif field_type == kFieldTypeFloat:
                return self._uint32_to_float(data)
                
            elif field_type == kFieldTypeDouble:
                return self._read_double(data)
                
            elif field_type == kFieldTypeExoString:
                return self._read_exo_string(data)
                
            elif field_type == kFieldTypeResRef:
                return self._read_resref(data)
                
            elif field_type == kFieldTypeLocString:
                return f"<LocString offset:{data}>"
                
            elif field_type == kFieldTypeVector:
                return self._read_vector(data)
                
            elif field_type == kFieldTypeOrientation:
                return self._read_orientation(data)
                
            elif field_type == kFieldTypeStruct:
                return data
                
            elif field_type == kFieldTypeList:
                return data
                
            elif field_type == kFieldTypeStrRef:
                return self._read_strref(data)
                
            else:
                return data
                
        except Exception as e:
            return f"<error: {e}>"
    
    def _uint32_to_float(self, uint_val):
        return struct.unpack('<f', struct.pack('<I', uint_val & 0xFFFFFFFF))[0]
    
    def _read_double(self, offset):
        field_data_offset = self.header.get('field_data_offset', 0)
        double_pos = field_data_offset + offset
        
        if double_pos + 8 > len(self.data):
            return 0.0
        
        saved_pos = self.pos
        self.pos = double_pos
        try:
            return self.read_double()
        except:
            return 0.0
        finally:
            self.pos = saved_pos
    
    def _read_exo_string(self, offset):
        field_data_offset = self.header.get('field_data_offset', 0)
        str_pos = field_data_offset + offset
        
        if str_pos + 4 > len(self.data):
            return "<string offset out of bounds>"
        
        saved_pos = self.pos
        self.pos = str_pos
        try:
            str_len = self.read_uint32()
            if str_len > 0 and str_len < 65536:  # Sanity check
                return self.read_string(str_len)
            else:
                return f"<invalid string length: {str_len}>"
        except:
            return "<error reading string>"
        finally:
            self.pos = saved_pos
    
    def _read_resref(self, offset):
        field_data_offset = self.header.get('field_data_offset', 0)
        resref_pos = field_data_offset + offset
        
        if resref_pos + 1 > len(self.data):
            return "<resref out of bounds>"
        
        saved_pos = self.pos
        self.pos = resref_pos
        try:
            str_len = self.read_byte()
            if str_len > 0 and str_len < 256:  # Sanity check
                return self.read_string(str_len)
            else:
                return ""
        except:
            return ""
        finally:
            self.pos = saved_pos
    
    def _read_vector(self, offset):
        field_data_offset = self.header.get('field_data_offset', 0)
        vec_pos = field_data_offset + offset
        
        if vec_pos + 12 > len(self.data):
            return (0.0, 0.0, 0.0)
        
        saved_pos = self.pos
        self.pos = vec_pos
        try:
            x = self.read_float()
            y = self.read_float()
            z = self.read_float()
            return (x, y, z)
        except:
            return (0.0, 0.0, 0.0)
        finally:
            self.pos = saved_pos
    
    def _read_orientation(self, offset):
        field_data_offset = self.header.get('field_data_offset', 0)
        orient_pos = field_data_offset + offset
        
        if orient_pos + 16 > len(self.data):
            return (0.0, 0.0, 0.0, 1.0)
        
        saved_pos = self.pos
        self.pos = orient_pos
        try:
            x = self.read_float()
            y = self.read_float()
            z = self.read_float()
            w = self.read_float()
            return (x, y, z, w)
        except:
            return (0.0, 0.0, 0.0, 1.0)
        finally:
            self.pos = saved_pos
    
    def _read_strref(self, offset):
        field_data_offset = self.header.get('field_data_offset', 0)
        strref_pos = field_data_offset + offset
        
        if strref_pos + 8 > len(self.data):
            return 0xFFFFFFFF
        
        saved_pos = self.pos
        self.pos = strref_pos
        try:
            size = self.read_uint32()
            if size == 4:
                return self.read_uint32()
            else:
                return 0xFFFFFFFF
        except:
            return 0xFFFFFFFF
        finally:
            self.pos = saved_pos


# ============
# Helper Functions
# ============

def cleanup_empty_collections():
    removed_count = 0
    collections_to_check = list(bpy.data.collections)
    
    for collection in collections_to_check:
        if len(collection.objects) > 0:
            continue

        has_objects_in_children = False
        for child in collection.children:
            if len(child.objects) > 0:
                has_objects_in_children = True
                break
        
        if has_objects_in_children:
            continue
            
        bpy.data.collections.remove(collection)
        removed_count += 1
    
    if removed_count > 0:
        print(f"    Removed {removed_count} empty collections")


def quaternion_to_euler(quat):
    """
    Convert a quaternion (x, y, z, w) to Euler angles (roll, pitch, yaw) in degrees
    Following the Z-Y-X convention (yaw, pitch, roll)
    """
    x, y, z, w = quat
    
    sinr_cosp = 2 * (w * x + y * z)
    cosr_cosp = 1 - 2 * (x * x + y * y)
    roll = math.atan2(sinr_cosp, cosr_cosp)
    
    sinp = 2 * (w * y - z * x)
    if abs(sinp) >= 1:
        pitch = math.copysign(math.pi / 2, sinp)
    else:
        pitch = math.asin(sinp)
    
    siny_cosp = 2 * (w * z + x * y)
    cosy_cosp = 1 - 2 * (y * y + z * z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    
    return (math.degrees(roll), math.degrees(pitch), math.degrees(yaw))


def witcher_to_blender_transform(position, quaternion):
    """
    Just subtract 1500 from X and Y
    """
    x, y, z = position
    qx, qy, qz, qw = quaternion
    
    blender_pos = (x - 1500.0, y - 1500.0, z)
    blender_q = Quaternion((qw, qx, qy, qz))
    
    return blender_pos, blender_q


def find_mdb_file(model_name, game_root):
    """Find the MDB file for a model name"""
    if not model_name:
        return None

    search_paths = [
        os.path.join(game_root, "meshes00"),
        os.path.join(game_root, "templates00"),
        os.path.join(game_root, "items00"),
    ]
    
    # Add the directory of the current file as a fallback
    if game_root:
        search_paths.append(game_root)
    
    for base_path in search_paths:
        if os.path.exists(base_path):
            for root, dirs, files in os.walk(base_path):
                for file in files:
                    if file.lower() == f"{model_name.lower()}.mdb":
                        full_path = os.path.join(root, file)
                        print(f"    Found MDB: {full_path}")
                        return full_path
    
    # Try direct path
    for base_path in search_paths:
        direct_path = os.path.join(base_path, f"{model_name}.mdb")
        if os.path.exists(direct_path):
            return direct_path
    
    return None


def create_placeholder_cube(position, quaternion, name, collection):
    """Create a placeholder cube when MDB model is not found"""
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=position)
    obj = bpy.context.active_object
    
    obj.rotation_mode = 'QUATERNION'
    obj.rotation_quaternion = quaternion
    
    obj.name = f"Placeholder_{name}"
    
    if "Placeholder_Mat" not in bpy.data.materials:
        mat = bpy.data.materials.new(name="Placeholder_Mat")
        mat.use_nodes = True
        mat.node_tree.nodes["Principled BSDF"].inputs[0].default_value = (1.0, 1.0, 0.0, 1.0)  # Yellow
    else:
        mat = bpy.data.materials["Placeholder_Mat"]
    
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    
    for col in obj.users_collection:
        col.objects.unlink(obj)
    collection.objects.link(obj)
    
    return obj


def create_door_placeholder(position, orientation, model_name, collection):
    """Create a placeholder for a door when model not found"""
    bpy.ops.mesh.primitive_cube_add(size=0.8, location=position)
    obj = bpy.context.active_object
    
    obj.rotation_mode = 'QUATERNION'
    obj.rotation_quaternion = Quaternion((
        orientation['w'],
        orientation['x'],
        orientation['y'],
        orientation['z']
    ))
    
    obj.name = f"Door_Placeholder_{model_name}"
    
    if "Door_Placeholder_Mat" not in bpy.data.materials:
        mat = bpy.data.materials.new(name="Door_Placeholder_Mat")
        mat.use_nodes = True
        mat.node_tree.nodes["Principled BSDF"].inputs[0].default_value = (0.0, 0.0, 1.0, 1.0)  # Blue
    else:
        mat = bpy.data.materials["Door_Placeholder_Mat"]
    
    if obj.data.materials:
        obj.data.materials[0] = mat
    else:
        obj.data.materials.append(mat)
    
    for col in obj.users_collection:
        col.objects.unlink(obj)
    collection.objects.link(obj)
    
    return obj


# ============
# Module Parser
# ============

class ModuleParser:
    """Main parser for Witcher .mod files"""
    
    def __init__(self, filepath, game_root=None, time_of_day='DAY'):
        self.filepath = filepath
        self.erf = ERFFile(filepath)
        self.gff = None
        self.objects = []
        self.doors = []
        self.game_root = game_root
        self.time_of_day = time_of_day
        self.areas = []
        
    def parse(self):
        print(f"\n{'#'*60}")
        print(f"# The Witcher 1 Module Parser")
        print(f"# File: {os.path.basename(self.filepath)}")
        print(f"{'#'*60}")
        
        # Parse ERF structure
        header, resources, description = self.erf.parse()
        self.areas = self.erf.areas
        
        print(f"\nModule Description: {description}")
        
        return self.areas
    
    def parse_area(self, area_name):
        """Parse a specific area by name and return objects"""
        print(f"\n{'='*60}")
        print(f"Parsing area: {area_name}")
        print(f"{'='*60}")
        
        # Get all resources for this area
        area_resources = self.erf.get_area_resources(area_name)
        
        are_res = None
        git_res = None
        
        for res in area_resources:
            if res['type'] == kFileTypeARE:
                are_res = res
            elif res['type'] == kFileTypeGIT:
                git_res = res
        
        # Parse ARE file
        tileset = ""
        area_localized_names = {}
        if are_res:
            print(f"\n  Area: {area_name}")
            are_data = self.erf.extract_resource(are_res['index'])
            if are_data and len(are_data) > 0:
                gff = GFF3File(are_data, f"{area_name}.are")
                are_struct = gff.parse()
                if are_struct:
                    fields = are_struct.get('fields', {})
                    
                    # Extract Tileset
                    if 'Tileset' in fields:
                        tileset_field = fields['Tileset']
                        if tileset_field['type'] == kFieldTypeResRef:
                            tileset = gff.get_field_value(tileset_field)
                            if tileset:
                                print(f"    Tileset: {tileset}")
        
        # Parse GIT file
        if git_res:
            git_data = self.erf.extract_resource(git_res['index'])
            if git_data and len(git_data) > 0:
                self.gff = GFF3File(git_data, f"{area_name}.git")
                git_struct = self.gff.parse()
                if git_struct:
                    self._parse_git_placeables_only(self.gff, git_struct, area_name)
        
        return self.objects, self.doors, tileset
    
    def _parse_git_placeables_only(self, gff, struct, area_name):
        """Parse GIT file contents - placeables, action points, and doors"""
        if not struct:
            return
            
        fields = struct.get('fields', {})
        
        objects = []
        
        # Parse Placeable List
        if 'Placeable List' in fields:
            list_field = fields['Placeable List']
            if list_field['type'] == kFieldTypeList:
                placeables = self._parse_object_list(gff, list_field['data'], area_name, "Placeables")
                objects.extend(placeables)
        
        # Parse Action Point List (ActPtList)
        if 'ActPtList' in fields:
            list_field = fields['ActPtList']
            if list_field['type'] == kFieldTypeList:
                action_points = self._parse_action_point_list(gff, list_field['data'], area_name)
                objects.extend(action_points)
        
        self.objects = objects
        
        # Parse Door List separately
        if 'Door List' in fields:
            list_field = fields['Door List']
            if list_field['type'] == kFieldTypeList:
                self.doors = self._parse_door_list(gff, list_field['data'], area_name)
    
    def _parse_object_list(self, gff, list_offset, area_name, list_type):
        """Parse a list of objects and extract their properties"""
        objects = []
        
        list_index = list_offset // 4
        list_indices_offset = gff.header.get('list_indices_offset', 0)
        
        # Read the list structure
        saved_pos = gff.pos
        gff.pos = list_indices_offset + list_index * 4
        
        try:
            # Read number of items in this list
            num_items = gff.read_uint32()
            
            if num_items == 0 or num_items > 10000:  # Sanity check
                return objects
            
            print(f"      {list_type}: {num_items} objects")
            
            # Read struct indices
            struct_indices = []
            for i in range(num_items):
                try:
                    struct_idx = gff.read_uint32()
                    struct_indices.append(struct_idx)
                except:
                    break
            
            # Parse each object struct
            for idx, struct_idx in enumerate(struct_indices):
                if struct_idx < len(gff.structs):
                    obj_struct = gff.structs[struct_idx]
                    
                    if obj_struct:
                        obj_data = self._extract_object_properties(obj_struct)
                        obj_data['area'] = area_name
                        obj_data['list_type'] = list_type[:-1]
                        objects.append(obj_data)
                        
                        if idx < 10:
                            self._print_object(obj_data, idx + 1)
                        elif idx == 10:
                            print(f"        ... and {num_items - 10} more objects")
                    
        except Exception as e:
            print(f"      Error parsing list: {e}")
        finally:
            gff.pos = saved_pos
        
        return objects

    def _parse_action_point_list(self, gff, list_offset, area_name):
        """Parse a list of action points and extract their properties"""
        objects = []
        
        list_index = list_offset // 4
        list_indices_offset = gff.header.get('list_indices_offset', 0)
        
        # Read the list structure
        saved_pos = gff.pos
        gff.pos = list_indices_offset + list_index * 4
        
        try:
            # Read number of items in this list
            num_items = gff.read_uint32()
            
            if num_items == 0 or num_items > 10000:  # Sanity check
                return objects
            
            print(f"      Action Points: {num_items} objects")
            
            # Read struct indices
            struct_indices = []
            for i in range(num_items):
                try:
                    struct_idx = gff.read_uint32()
                    struct_indices.append(struct_idx)
                except:
                    break
            
            # Parse each action point struct
            for idx, struct_idx in enumerate(struct_indices):
                if struct_idx < len(gff.structs):
                    obj_struct = gff.structs[struct_idx]
                    
                    if obj_struct:
                        obj_data = self._extract_action_point_properties(obj_struct)
                        obj_data['area'] = area_name
                        obj_data['list_type'] = 'ActionPoint'
                        objects.append(obj_data)
                        
                        if idx < 10:
                            self._print_action_point(obj_data, idx + 1)
                        elif idx == 10:
                            print(f"        ... and {num_items - 10} more action points")
                        
        except Exception as e:
            print(f"      Error parsing action point list: {e}")
        finally:
            gff.pos = saved_pos
        
        return objects

    def _parse_action_list(self, list_offset):
        """Parse the Actions list from an action point"""
        actions = []
        
        list_index = list_offset // 4
        list_indices_offset = self.gff.header.get('list_indices_offset', 0)
        
        saved_pos = self.gff.pos
        self.gff.pos = list_indices_offset + list_index * 4
        
        try:
            num_items = self.gff.read_uint32()
            
            if num_items == 0 or num_items > 100:
                return actions
            
            struct_indices = []
            for i in range(num_items):
                try:
                    struct_idx = self.gff.read_uint32()
                    struct_indices.append(struct_idx)
                except:
                    break
            
            for struct_idx in struct_indices:
                if struct_idx < len(self.gff.structs):
                    action_struct = self.gff.structs[struct_idx]
                    if action_struct:
                        fields = action_struct.get('fields', {})
                        if 'vecelem' in fields:
                            vecelem_field = fields['vecelem']
                            if vecelem_field['type'] == kFieldTypeExoString:
                                action_name = self.gff.get_field_value(vecelem_field)
                                if action_name:
                                    actions.append(action_name)
        
        except Exception as e:
            print(f"        Error parsing action list: {e}")
        finally:
            self.gff.pos = saved_pos
        
        return actions

    def _parse_door_list(self, gff, list_offset, area_name):
        """Parse a list of doors and extract their properties"""
        doors = []
        
        list_index = list_offset // 4
        list_indices_offset = gff.header.get('list_indices_offset', 0)
        
        # Read the list structure
        saved_pos = gff.pos
        gff.pos = list_indices_offset + list_index * 4
        
        try:
            # Read number of items in this list
            num_items = gff.read_uint32()
            
            if num_items == 0 or num_items > 10000:  # Sanity check
                return doors
            
            print(f"      Doors: {num_items} objects")
            
            # Read struct indices
            struct_indices = []
            for i in range(num_items):
                try:
                    struct_idx = gff.read_uint32()
                    struct_indices.append(struct_idx)
                except:
                    break
            
            # Parse each door struct
            for idx, struct_idx in enumerate(struct_indices):
                if struct_idx < len(gff.structs):
                    obj_struct = gff.structs[struct_idx]
                    
                    if obj_struct:
                        door_data = self._extract_door_properties(obj_struct)
                        door_data['area'] = area_name
                        doors.append(door_data)
                        
                        if idx < 10:
                            self._print_door(door_data, idx + 1)
                        elif idx == 10:
                            print(f"        ... and {num_items - 10} more doors")
                        
        except Exception as e:
            print(f"      Error parsing door list: {e}")
        finally:
            gff.pos = saved_pos
        
        return doors
    
    def _extract_object_properties(self, obj_struct):
        """Extract position, rotation, and other properties from an object"""
        obj_data = {
            'tag': '',
            'template': '',
            'model_name': '',
            'position': (0.0, 0.0, 0.0),
            'orientation': (0.0, 0.0, 0.0, 1.0),
            'scale': 1.0,
            'appearance_id': 0, 
            'has_inventory': False,
            'properties': {}
        }
        
        fields = obj_struct.get('fields', {})
        
        # Extract tag
        if 'Tag' in fields:
            tag_field = fields['Tag']
            if tag_field['type'] == kFieldTypeExoString:
                tag_value = self.gff.get_field_value(tag_field)
                if tag_value and not tag_value.startswith("<invalid string length"):
                    obj_data['tag'] = tag_value
        
        # Extract model name directly from GIT (highest priority)
        git_model_name = None
        if 'ModelName' in fields:
            model_field = fields['ModelName']
            if model_field['type'] == kFieldTypeExoString:
                git_model_name = self.gff.get_field_value(model_field)
                if git_model_name and not git_model_name.startswith("<invalid"):
                    obj_data['model_name'] = git_model_name
                    print(f"      Found ModelName in GIT: {git_model_name}")
        
        # Extract template reference
        if 'TemplateResRef' in fields:
            template_field = fields['TemplateResRef']
            if template_field['type'] == kFieldTypeResRef:
                template_name = self.gff.get_field_value(template_field)
                obj_data['template'] = template_name
                
                if not git_model_name:
                    obj_data['model_name'] = template_name
 
                print(f"      Using GIT ModelName: {git_model_name} (template: {template_name})")
        
        # EXTRACT MODEL SCALE
        if 'ModelScale' in fields:
            scale_field = fields['ModelScale']
            if scale_field['type'] == kFieldTypeFloat:
                obj_data['scale'] = self.gff.get_field_value(scale_field)
        
        # EXTRACT POSITION - Placeables use X, Y, Z
        x = y = z = None
        
        if 'X' in fields:
            pos_field = fields['X']
            if pos_field['type'] == kFieldTypeFloat:
                x = self.gff.get_field_value(pos_field)
        if 'Y' in fields:
            pos_field = fields['Y']
            if pos_field['type'] == kFieldTypeFloat:
                y = self.gff.get_field_value(pos_field)
        if 'Z' in fields:
            pos_field = fields['Z']
            if pos_field['type'] == kFieldTypeFloat:
                z = self.gff.get_field_value(pos_field)
        
        if x is not None and y is not None and z is not None:
            obj_data['position'] = (x, y, z)
        
        # Alternative: single Position field (vector)
        if obj_data['position'] == (0.0, 0.0, 0.0) and 'Position' in fields:
            pos_field = fields['Position']
            if pos_field['type'] == kFieldTypeVector:
                pos = self.gff.get_field_value(pos_field)
                if isinstance(pos, tuple) and len(pos) == 3:
                    obj_data['position'] = pos
        
        # EXTRACT ORIENTATION - Placeables use quaternion fields or Bearing
        ox = oy = oz = ow = None
        
        if 'OrientationX' in fields:
            orient_field = fields['OrientationX']
            if orient_field['type'] == kFieldTypeFloat:
                ox = self.gff.get_field_value(orient_field)
        if 'OrientationY' in fields:
            orient_field = fields['OrientationY']
            if orient_field['type'] == kFieldTypeFloat:
                oy = self.gff.get_field_value(orient_field)
        if 'OrientationZ' in fields:
            orient_field = fields['OrientationZ']
            if orient_field['type'] == kFieldTypeFloat:
                oz = self.gff.get_field_value(orient_field)
        if 'OrientationW' in fields:
            orient_field = fields['OrientationW']
            if orient_field['type'] == kFieldTypeFloat:
                ow = self.gff.get_field_value(orient_field)
        
        if ox is not None and oy is not None and oz is not None and ow is not None:
            obj_data['orientation'] = (ox, oy, oz, ow)
        
        elif 'Bearing' in fields:
            bearing_field = fields['Bearing']
            if bearing_field['type'] == kFieldTypeFloat:
                bearing = self.gff.get_field_value(bearing_field)
                obj_data['orientation'] = (0.0, 0.0, math.sin(bearing/2), math.cos(bearing/2))
        
        # Alternative: single Orientation field
        if obj_data['orientation'] == (0.0, 0.0, 0.0, 1.0) and 'Orientation' in fields:
            orient_field = fields['Orientation']
            if orient_field['type'] == kFieldTypeOrientation:
                orient = self.gff.get_field_value(orient_field)
                if isinstance(orient, tuple) and len(orient) == 4:
                    obj_data['orientation'] = orient
        
        # Store other interesting properties
        for field_name, field in fields.items():
            if field_name not in ['Tag', 'TemplateResRef', 'X', 'Y', 'Z', 'Position', 
                                  'OrientationX', 'OrientationY', 'OrientationZ', 
                                  'OrientationW', 'Orientation', 'Bearing', 'ModelScale',
                                  'ModelName']:
                if field['type'] in [kFieldTypeExoString, kFieldTypeResRef, kFieldTypeFloat,
                                     kFieldTypeUint32, kFieldTypeSint32, kFieldTypeByte,
                                     kFieldTypeUint16, kFieldTypeSint16]:
                    value = self.gff.get_field_value(field)
                    if value and value != "" and value != 0:
                        obj_data['properties'][field_name] = value
        
        return obj_data

    def _extract_action_point_properties(self, obj_struct):
        """Extract position, rotation, and other properties from an action point"""
        obj_data = {
            'name': '',
            'tag': '',
            'model_name': '',  # From AppearObjResRef
            'position': (0.0, 0.0, 0.0),
            'orientation': (0.0, 0.0, 0.0, 1.0),
            'scale': 1.0,
            'actions': [],
            'properties': {}
        }
        
        fields = obj_struct.get('fields', {})
        
        # name
        if 'Name' in fields:
            name_field = fields['Name']
            if name_field['type'] == kFieldTypeExoString:
                name_value = self.gff.get_field_value(name_field)
                if name_value and not name_value.startswith("<invalid"):
                    obj_data['name'] = name_value
        
        # tag
        if 'Tag' in fields:
            tag_field = fields['Tag']
            if tag_field['type'] == kFieldTypeExoString:
                tag_value = self.gff.get_field_value(tag_field)
                if tag_value and not tag_value.startswith("<invalid"):
                    obj_data['tag'] = tag_value
        
        # model name
        if 'AppearObjResRef' in fields:
            model_field = fields['AppearObjResRef']
            if model_field['type'] == kFieldTypeResRef:
                model_name = self.gff.get_field_value(model_field)
                if model_name:
                    obj_data['model_name'] = model_name
                    print(f"      Found AppearObjResRef: {model_name}")
        
        # actions
        if 'Actions' in fields:
            actions_field = fields['Actions']
            if actions_field['type'] == kFieldTypeList:
                actions = self._parse_action_list(actions_field['data'])
                obj_data['actions'] = actions
        
        # POSITION
        x = y = z = None
        
        if 'PositionX' in fields:
            pos_field = fields['PositionX']
            if pos_field['type'] == kFieldTypeFloat:
                x = self.gff.get_field_value(pos_field)
        if 'PositionY' in fields:
            pos_field = fields['PositionY']
            if pos_field['type'] == kFieldTypeFloat:
                y = self.gff.get_field_value(pos_field)
        if 'PositionZ' in fields:
            pos_field = fields['PositionZ']
            if pos_field['type'] == kFieldTypeFloat:
                z = self.gff.get_field_value(pos_field)
        
        if x is not None and y is not None and z is not None:
            obj_data['position'] = (x, y, z)
        
        # ORIENTATION
        ox = oy = oz = ow = None
        
        if 'OrientationX' in fields:
            orient_field = fields['OrientationX']
            if orient_field['type'] == kFieldTypeFloat:
                ox = self.gff.get_field_value(orient_field)
        if 'OrientationY' in fields:
            orient_field = fields['OrientationY']
            if orient_field['type'] == kFieldTypeFloat:
                oy = self.gff.get_field_value(orient_field)
        if 'OrientationZ' in fields:
            orient_field = fields['OrientationZ']
            if orient_field['type'] == kFieldTypeFloat:
                oz = self.gff.get_field_value(orient_field)
        if 'OrientationW' in fields:
            orient_field = fields['OrientationW']
            if orient_field['type'] == kFieldTypeFloat:
                ow = self.gff.get_field_value(orient_field)
        
        if ox is not None and oy is not None and oz is not None and ow is not None:
            obj_data['orientation'] = (ox, oy, oz, ow)
        
        # Store other interesting properties
        for field_name, field in fields.items():
            if field_name not in ['Name', 'Tag', 'AppearObjResRef', 'Actions', 
                                  'PositionX', 'PositionY', 'PositionZ',
                                  'OrientationX', 'OrientationY', 'OrientationZ', 'OrientationW']:
                if field['type'] in [kFieldTypeExoString, kFieldTypeResRef, kFieldTypeFloat,
                                     kFieldTypeUint32, kFieldTypeSint32, kFieldTypeByte,
                                     kFieldTypeUint16, kFieldTypeSint16]:
                    value = self.gff.get_field_value(field)
                    if value and value != "" and value != 0:
                        obj_data['properties'][field_name] = value
        
        return obj_data

    def _extract_door_properties(self, obj_struct):
        """Extract position, rotation, and other properties from a door"""
        door_data = {
            'tag': '',
            'template': '',
            'model_name': '',
            'position': (0.0, 0.0, 0.0),
            'orientation': (0.0, 0.0, 0.0, 1.0),
            'scale': 1.0,
            'unique_id': '',
            'properties': {}
        }
        
        fields = obj_struct.get('fields', {})
        
        # tag
        if 'Tag' in fields:
            tag_field = fields['Tag']
            if tag_field['type'] == kFieldTypeExoString:
                tag_value = self.gff.get_field_value(tag_field)
                if tag_value and not tag_value.startswith("<invalid"):
                    door_data['tag'] = tag_value
        
        # template
        if 'TemplateResRef' in fields:
            template_field = fields['TemplateResRef']
            if template_field['type'] == kFieldTypeResRef:
                template_value = self.gff.get_field_value(template_field)
                if template_value:
                    door_data['template'] = template_value
        
        # model name
        if 'ModelName' in fields:
            model_field = fields['ModelName']
            if model_field['type'] == kFieldTypeExoString:
                model_value = self.gff.get_field_value(model_field)
                if model_value and not model_value.startswith("<invalid"):
                    door_data['model_name'] = model_value
                    print(f"      Door ModelName: {model_value}")
        
        # unique ID
        if 'UniqueID' in fields:
            uid_field = fields['UniqueID']
            if uid_field['type'] == kFieldTypeExoString:
                uid_value = self.gff.get_field_value(uid_field)
                if uid_value:
                    door_data['unique_id'] = uid_value
        
        # POSITION
        x = y = z = None
        
        if 'X' in fields:
            pos_field = fields['X']
            if pos_field['type'] == kFieldTypeFloat:
                x = self.gff.get_field_value(pos_field)
        if 'Y' in fields:
            pos_field = fields['Y']
            if pos_field['type'] == kFieldTypeFloat:
                y = self.gff.get_field_value(pos_field)
        if 'Z' in fields:
            pos_field = fields['Z']
            if pos_field['type'] == kFieldTypeFloat:
                z = self.gff.get_field_value(pos_field)
        
        if x is not None and y is not None and z is not None:
            door_data['position'] = (x, y, z)
        
        # ORIENTATION - Doors use Bearing (in radians) for rotation around Z
        if 'Bearing' in fields:
            bearing_field = fields['Bearing']
            if bearing_field['type'] == kFieldTypeFloat:
                bearing = self.gff.get_field_value(bearing_field)
                door_data['orientation'] = (0.0, 0.0, math.sin(bearing/2), math.cos(bearing/2))
        
        # Also check for full quaternion if available
        ox = oy = oz = ow = None
        
        if 'OrientationX' in fields:
            orient_field = fields['OrientationX']
            if orient_field['type'] == kFieldTypeFloat:
                ox = self.gff.get_field_value(orient_field)
        if 'OrientationY' in fields:
            orient_field = fields['OrientationY']
            if orient_field['type'] == kFieldTypeFloat:
                oy = self.gff.get_field_value(orient_field)
        if 'OrientationZ' in fields:
            orient_field = fields['OrientationZ']
            if orient_field['type'] == kFieldTypeFloat:
                oz = self.gff.get_field_value(orient_field)
        if 'OrientationW' in fields:
            orient_field = fields['OrientationW']
            if orient_field['type'] == kFieldTypeFloat:
                ow = self.gff.get_field_value(orient_field)
        
        if ox is not None and oy is not None and oz is not None and ow is not None:
            door_data['orientation'] = (ox, oy, oz, ow)
        
        # Store other interesting properties
        for field_name, field in fields.items():
            if field_name not in ['Tag', 'TemplateResRef', 'ModelName', 'UniqueID',
                                  'X', 'Y', 'Z', 'Bearing',
                                  'OrientationX', 'OrientationY', 'OrientationZ', 'OrientationW']:
                if field['type'] in [kFieldTypeExoString, kFieldTypeResRef, kFieldTypeFloat,
                                     kFieldTypeUint32, kFieldTypeSint32, kFieldTypeByte,
                                     kFieldTypeUint16, kFieldTypeSint16]:
                    value = self.gff.get_field_value(field)
                    if value and value != "" and value != 0:
                        door_data['properties'][field_name] = value
        
        return door_data

    def _print_object(self, obj_data, index):
        """Print object details"""
        pos = obj_data['position']
        orient = obj_data['orientation']
        
        blender_pos, blender_q = witcher_to_blender_transform(pos, orient)
        roll, pitch, yaw = quaternion_to_euler((blender_q.x, blender_q.y, blender_q.z, blender_q.w))
        
        tag = obj_data['tag'] or '<no tag>'
        if len(tag) > 30:
            tag = tag[:27] + "..."
        
        template = obj_data['template'] or '<none>'
        if len(template) > 30:
            template = template[:27] + "..."
        
        print(f"        Object {index}: {tag}")
        if template != '<none>':
            print(f"          Template: {template}")
        print(f"          Witcher Pos: ({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f})")
        print(f"          Blender Pos: ({blender_pos[0]:.2f}, {blender_pos[1]:.2f}, {blender_pos[2]:.2f})")
        
        if abs(orient[0]) > 0.001 or abs(orient[1]) > 0.001 or abs(orient[2]) > 0.001 or abs(orient[3] - 1.0) > 0.001:
            print(f"          Witcher Quat: ({orient[0]:.3f}, {orient[1]:.3f}, {orient[2]:.3f}, {orient[3]:.3f})")
            print(f"          Blender Euler: ({roll:.1f}°, {pitch:.1f}°, {yaw:.1f}°)")
        
        for prop_name in ['Appearance', 'Faction', 'Plot', 'Locked', 'HP', 'Hardness']:
            if prop_name in obj_data['properties']:
                print(f"          {prop_name}: {obj_data['properties'][prop_name]}")

    def _print_action_point(self, obj_data, index):
        """Print action point details"""
        pos = obj_data['position']
        orient = obj_data['orientation']
        
        blender_pos, blender_q = witcher_to_blender_transform(pos, orient)
        roll, pitch, yaw = quaternion_to_euler((blender_q.x, blender_q.y, blender_q.z, blender_q.w))

        name = obj_data['name'] or '<no name>'
        if len(name) > 30:
            name = name[:27] + "..."
        
        tag = obj_data['tag'] or '<no tag>'
        if len(tag) > 20:
            tag = tag[:17] + "..."
        
        print(f"        Action Point {index}: {name}")
        if tag != '<no tag>':
            print(f"          Tag: {tag}")
        if obj_data['model_name']:
            print(f"          Model: {obj_data['model_name']}")
        if obj_data['actions']:
            print(f"          Actions: {', '.join(obj_data['actions'][:3])}" + 
                  (f" and {len(obj_data['actions'])-3} more" if len(obj_data['actions']) > 3 else ""))
        print(f"          Witcher Pos: ({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f})")
        print(f"          Blender Pos: ({blender_pos[0]:.2f}, {blender_pos[1]:.2f}, {blender_pos[2]:.2f})")
        
        if abs(orient[0]) > 0.001 or abs(orient[1]) > 0.001 or abs(orient[2]) > 0.001 or abs(orient[3] - 1.0) > 0.001:
            print(f"          Blender Euler: ({roll:.1f}°, {pitch:.1f}°, {yaw:.1f}°)")

    def _print_door(self, door_data, index):
        """Print door details"""
        pos = door_data['position']
        orient = door_data['orientation']
        
        blender_pos, blender_q = witcher_to_blender_transform(pos, orient)
        roll, pitch, yaw = quaternion_to_euler((blender_q.x, blender_q.y, blender_q.z, blender_q.w))

        tag = door_data['tag'] or '<no tag>'
        if len(tag) > 30:
            tag = tag[:27] + "..."
        
        print(f"        Door {index}: {tag}")
        print(f"          Model: {door_data['model_name']}")
        print(f"          Template: {door_data['template']}")
        print(f"          UniqueID: {door_data['unique_id']}")
        print(f"          Witcher Pos: ({pos[0]:.2f}, {pos[1]:.2f}, {pos[2]:.2f})")
        print(f"          Blender Pos: ({blender_pos[0]:.2f}, {blender_pos[1]:.2f}, {blender_pos[2]:.2f})")
        
        # Only print orientation if it's not default
        if abs(orient[0]) > 0.001 or abs(orient[1]) > 0.001 or abs(orient[2]) > 0.001 or abs(orient[3] - 1.0) > 0.001:
            print(f"          Blender Euler: ({roll:.1f}°, {pitch:.1f}°, {yaw:.1f}°)")


# ============
# Main Import Operator
# ============

class IMPORT_WITCHER_MOD_OT_operator(Operator, ImportHelper):
    """Import The Witcher 1 .mod file and place objects"""
    
    bl_idname = "import_scene.witcher_mod"
    bl_label = "Import Witcher MOD"
    bl_options = {'REGISTER', 'UNDO'}
    
    filter_glob: StringProperty(
        default="*.mod;*.adv",
        options={'HIDDEN'},
    )

    time_of_day: EnumProperty(
        name="Time of Day",
        description="Select time of day for lightmaps",
        items=[
            ('DAY', "Day", "Use day lightmaps"),
            ('MORNING', "Morning", "Use morning lightmaps"),
            ('NOON', "Noon", "Use noon lightmaps"),
            ('EVENING', "Evening", "Use evening lightmaps"),
            ('NIGHT', "Night", "Use night lightmaps"),
            ('NONE', "None", "Don't load lightmaps"),
        ],
        default='MORNING',
    )
    
    current_language: IntProperty(
        name="Current Language",
        default=6,
        options={'HIDDEN'}
    )
    
    selected_language: EnumProperty(
        name="Display Language",
        description="Language to display area names in",
        items=LANGUAGE_ITEMS,
        default=DEFAULT_LANGUAGE,
        update=lambda self, context: setattr(self, 'current_language', int(self.selected_language))
    )
    
    load_mdb_models: BoolProperty(
        name="Load MDB Models",
        description="Load MDB models instead of placeholder cubes",
        default=True,
    )
    
    load_level_mesh: BoolProperty(
        name="Load Level Mesh",
        description="Load the actual level mesh for this area",
        default=True,
    )
    
    import_speedtrees: BoolProperty(
        name="Import SpeedTrees",
        description="Import SpeedTree objects",
        default=True,
    )
    
    create_placeholders: BoolProperty(
        name="Create Placeholders",
        description="Create placeholder cubes for objects without MDB files",
        default=False,
    )

    areas: CollectionProperty(type=AreaInfo)
    selected_area: IntProperty(name="Selected Area", default=0)
    module_description: StringProperty(name="Module Description", default="")
    
    # Flag to indicate if we're in area selection mode
    selecting_area: BoolProperty(default=False)
    mod_filepath: StringProperty(default="")
    
    def _update_ui(self, context):
        """Update UI when language changes"""
        if hasattr(context, 'area'):
            context.area.tag_redraw()
    
    def draw(self, context):
        layout = self.layout
        
        if self.selecting_area:
            # Area selection dialog
            layout.label(text=f"Module: {os.path.basename(self.mod_filepath)}")
            if self.module_description:
                layout.label(text=f"Description: {self.module_description}")
            layout.separator()
            
            row = layout.row()
            row.label(text="Display Language:")
            row.prop(self, "selected_language", text="")
            
            layout.separator()
            layout.label(text="Select area to import:")
            
            row = layout.row()
            row.template_list("AREA_UL_areas", "", self, "areas", self, "selected_area", rows=12)
            
            layout.separator()
            
            # Selected area info
            if len(self.areas) > 0 and self.selected_area < len(self.areas):
                area = self.areas[self.selected_area]
                row = layout.row()
                row.label(text=f"Selected: {area.name}")
                
                selected_lang = int(self.selected_language)
                if area.localized_names and str(selected_lang) in area.localized_names:
                    row.label(text=f"({area.localized_names[str(selected_lang)]})")
                elif area.localized_name:
                    row.label(text=f"({area.localized_name})")
            
            layout.separator()
            layout.prop(self, "time_of_day")
            layout.prop(self, "load_mdb_models")
            layout.prop(self, "load_level_mesh")
            
            if self.load_level_mesh:
                layout.prop(self, "import_speedtrees")
            
            layout.prop(self, "create_placeholders")
            
        else:
            # Initial file selection dialog
            layout.prop(self, "time_of_day")
            layout.prop(self, "load_mdb_models")
            layout.prop(self, "load_level_mesh")
            
            if self.load_level_mesh:
                layout.prop(self, "import_speedtrees")
            
            layout.prop(self, "create_placeholders")
    
    def reset_state(self):
        """Reset operator state for a new import"""
        self.selecting_area = False
        self.mod_filepath = ""
        self.module_description = ""
        self.selected_area = 0
        self.areas.clear()
    
    def invoke(self, context, event):
        """Reset state and show file browser"""
        self.reset_state()
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}
    
    def execute(self, context):
        # Get game path from addon preferences
        preferences = context.preferences.addons[__name__].preferences
        game_path = preferences.game_path
        
        if not self.selecting_area:
            # First step: parse the MOD file and show area selection
            print(f"\n{'='*60}")
            print(f"Parsing MOD file for area selection: {self.filepath}")
            print(f"{'='*60}")
            
            parser = ModuleParser(self.filepath, game_path, self.time_of_day)
            areas = parser.parse()
            self.module_description = parser.erf.description
            self.areas.clear()
            
            # Populate areas
            for area_info in areas:
                area = self.areas.add()
                area.name = area_info['name']
                area.has_are = area_info['has_are']
                area.has_git = area_info['has_git']
                area.tileset = area_info.get('tileset', '')
                area.resource_count = area_info['resource_count']
                
                # Store all localized names
                localized_names = area_info.get('localized_names', {})
                area.localized_names = localized_names
                
                # Get best localized name for display
                best_name = area_info.get('best_name', '')
                if best_name:
                    area.localized_name = best_name
                
                area.name_count = len(localized_names)
                available_langs = []
                for lang_id, text in localized_names.items():
                    if text:
                        lang_name = LANGUAGE_NAMES.get(lang_id, f"Lang{lang_id}")
                        available_langs.append(f"{lang_name}")
                        if len(available_langs) >= 10:
                            available_langs.append("...")
                            break
                
                area.available_languages = ', '.join(available_langs)
            
            if len(self.areas) == 0:
                self.report({'ERROR'}, "No areas found in MOD file")
                return {'CANCELLED'}
            
            # Switch to area selection mode
            self.selecting_area = True
            self.mod_filepath = self.filepath
            
            # Reopen the dialog in area selection mode
            context.window_manager.invoke_props_dialog(self, width=700)
            return {'RUNNING_MODAL'}
        
        else:
            # Second step: import the selected area
            if self.selected_area >= len(self.areas):
                self.report({'ERROR'}, "No area selected")
                return {'CANCELLED'}
            
            area_info = self.areas[self.selected_area]
            area_name = area_info.name
            tileset = area_info.tileset
            
            # Get localized name in selected language
            selected_lang = int(self.selected_language)
            if area_info.localized_names and str(selected_lang) in area_info.localized_names:
                localized_name = area_info.localized_names[str(selected_lang)]
            else:
                localized_name = area_info.localized_name
            
            print(f"\n{'='*60}")
            print(f"Importing area: {area_name}")
            if localized_name:
                print(f"Localized name: {localized_name} (Language: {LANGUAGE_NAMES.get(selected_lang, 'Unknown')})")
            if tileset:
                print(f"Tileset: {tileset}")
            print(f"{'='*60}")
            
            if not MDB_IMPORTER_AVAILABLE and self.load_mdb_models:
                self.report({'WARNING'}, "MDB importer not found. Will use placeholder cubes.")
                self.load_mdb_models = False
            
            parser = ModuleParser(
                self.mod_filepath,
                game_path,
                self.time_of_day
            )
            parser.parse()
            objects, doors, parsed_tileset = parser.parse_area(area_name)

            if not tileset and parsed_tileset:
                tileset = parsed_tileset
            
            # Create main collection for this area
            main_collection_name = f"TW1_{area_name}"
            if localized_name:
                clean_name = "".join(c for c in localized_name if c.isalnum() or c in (' ', '-', '_')).strip()
                if clean_name:
                    main_collection_name = f"TW1_{area_name}_{clean_name}"
            
            if main_collection_name in bpy.data.collections:
                main_collection = bpy.data.collections[main_collection_name]
                for obj in main_collection.objects:
                    bpy.data.objects.remove(obj, do_unlink=True)
            else:
                main_collection = bpy.data.collections.new(main_collection_name)
                context.scene.collection.children.link(main_collection)
            
            collections = {}
            
            # Level mesh collection
            level_collection_name = f"{main_collection_name}_Level"
            if level_collection_name in bpy.data.collections:
                level_collection = bpy.data.collections[level_collection_name]
            else:
                level_collection = bpy.data.collections.new(level_collection_name)
                main_collection.children.link(level_collection)
            collections['level'] = level_collection
            
            # Placeables collection
            placeables_collection_name = f"{main_collection_name}_Placeables"
            if placeables_collection_name in bpy.data.collections:
                placeables_collection = bpy.data.collections[placeables_collection_name]
            else:
                placeables_collection = bpy.data.collections.new(placeables_collection_name)
                main_collection.children.link(placeables_collection)
            collections['placeables'] = placeables_collection
            
            # Doors collection
            doors_collection_name = f"{main_collection_name}_Doors"
            if doors_collection_name in bpy.data.collections:
                doors_collection = bpy.data.collections[doors_collection_name]
            else:
                doors_collection = bpy.data.collections.new(doors_collection_name)
                main_collection.children.link(doors_collection)
            collections['doors'] = doors_collection
            
            # Make the main collection active
            layer_collection = context.view_layer.layer_collection
            for lc in layer_collection.children:
                if lc.name == main_collection_name:
                    context.view_layer.active_layer_collection = lc
                    break

            print(f"\n{'#'*60}")
            print(f"# Found {len(objects)} placeables in {area_name}.git")
            print(f"{'#'*60}")
            
            if len(objects) == 0:
                self.report({'WARNING'}, f"No placeables found in {area_name}.git")
                return {'CANCELLED'}
            
            # Filter out fx_ templates
            filtered_objects = []
            fx_count = 0
            for obj in objects:
                if obj.get('list_type') == 'Placeable':
                    template = obj.get('template', '')
                    if template and template.startswith('fx_'):
                        fx_count += 1
                        print(f"    Skipping fx_ object: {template}")
                        continue
                filtered_objects.append(obj)

            print(f"\n    Filtered out {fx_count} fx_ objects, keeping {len(filtered_objects)} total objects")
            
            print(f"\n{'='*60}")
            print(f"Importing Placeables")
            print(f"{'='*60}")

            placeables = [obj for obj in filtered_objects if obj.get('list_type') == 'Placeable']
            action_points = [obj for obj in filtered_objects if obj.get('list_type') == 'ActionPoint']

            print(f"    Found {len(placeables)} placeables and {len(action_points)} action points")

            # Track loaded MDB files to avoid duplicates
            loaded_mdbs = {}
            placeholder_count = 0
            mdb_count = 0

            # Process placeables
            for i, obj_data in enumerate(placeables):
                pos = obj_data['position']
                orient = obj_data['orientation']
                scale = obj_data['scale']
                template = obj_data.get('template', '')
                tag = obj_data.get('tag', '')
                
                display_name = tag
                if not display_name or display_name.startswith("<invalid"):
                    display_name = template or f"placeable_{i:03d}"
                
                model_name = obj_data.get('model_name', template)

                blender_pos, blender_q = witcher_to_blender_transform(pos, orient)
                
                obj = None
                
                # Try to load MDB model
                if self.load_mdb_models and model_name:
                    cache_key = model_name
                    
                    if cache_key in loaded_mdbs:
                        source_obj = loaded_mdbs[cache_key]
                        if source_obj:
                            obj = bpy.data.objects.new(f"Placeable_{display_name}", None)
                            obj.empty_display_size = 0.5
                            obj.empty_display_type = 'CUBE'
                            placeables_collection.objects.link(obj)
                            
                            for child in source_obj.children:
                                new_child = child.copy()
                                new_child.data = child.data.copy() if child.data else None
                                new_child.parent = obj
                                placeables_collection.objects.link(new_child)
                            
                            print(f"    Duplicated {model_name} for {display_name}")
                            mdb_count += 1
                    else:
                        # Find and load MDB file
                        mdb_path = find_mdb_file(model_name, game_path)
                        if mdb_path:
                            print(f"    Loading MDB for {display_name} (model: {model_name}): {os.path.basename(mdb_path)}")
                            imported_objects = load_mdb_model(
                                mdb_path, 
                                game_path, 
                                self.time_of_day,
                                import_speedtrees=True
                            )
                            
                            if imported_objects:
                                # Create a parent empty to hold all meshes
                                obj = bpy.data.objects.new(f"Placeable_{display_name}", None)
                                obj.empty_display_size = 0.5
                                obj.empty_display_type = 'CUBE'
                                placeables_collection.objects.link(obj)
                                
                                loaded_mdbs[cache_key] = obj
                                
                                # Parent all imported objects to this empty
                                for imported_obj in imported_objects:
                                    for col in imported_obj.users_collection:
                                        col.objects.unlink(imported_obj)
                                    placeables_collection.objects.link(imported_obj)
                                    imported_obj.parent = obj
                                    
                                mdb_count += 1
                                print(f"      Loaded {len(imported_objects)} meshes")
                            else:
                                print(f"      Failed to load MDB for {model_name}")
                        else:
                            print(f"      MDB file not found for model: {model_name}")
                
                # Create placeholder if needed
                if obj is None and self.create_placeholders:
                    obj = create_placeholder_cube(blender_pos, blender_q, display_name, placeables_collection)
                    placeholder_count += 1
                
                if obj:
                    obj.location = blender_pos
                    obj.rotation_mode = 'QUATERNION'
                    obj.rotation_quaternion = blender_q
                    obj.scale = (scale, scale, scale)
                    
                    # Store properties as custom attributes
                    obj["Witcher_Type"] = "Placeable"
                    obj["Witcher_Template"] = template or ""
                    obj["Witcher_Tag"] = tag or ""
                    obj["Witcher_Model"] = model_name
                    obj["Witcher_Scale"] = scale
                    for prop_name, prop_value in obj_data.get('properties', {}).items():
                        obj[f"Witcher_{prop_name}"] = str(prop_value)
                
                if (i + 1) % 50 == 0 or i == len(placeables) - 1:
                    print(f"    Processed {i + 1}/{len(placeables)} placeables")

            # Process action points
            print(f"\n{'='*60}")
            print(f"Importing Action Points")
            print(f"{'='*60}")

            action_points_collection_name = f"{main_collection_name}_ActionPoints"
            if action_points_collection_name in bpy.data.collections:
                action_points_collection = bpy.data.collections[action_points_collection_name]
            else:
                action_points_collection = bpy.data.collections.new(action_points_collection_name)
                main_collection.children.link(action_points_collection)

            action_point_count = 0
            action_point_placeholder_count = 0

            for i, obj_data in enumerate(action_points):
                pos = obj_data['position']
                orient = obj_data['orientation']
                model_name = obj_data.get('model_name', '')
                name = obj_data.get('name', f"action_point_{i:03d}")
                tag = obj_data.get('tag', '')
                
                if not model_name:
                    print(f"    Skipping action point {name} - no model specified")
                    continue
                
                blender_pos, blender_q = witcher_to_blender_transform(pos, orient)
                
                obj = None
                
                # Try to load MDB model
                if self.load_mdb_models:
                    cache_key = model_name
                    if cache_key in loaded_mdbs:
                        source_obj = loaded_mdbs[cache_key]
                        if source_obj:
                            # Create a new parent empty
                            display_name = tag if tag and not tag.startswith("<invalid") else name
                            obj = bpy.data.objects.new(f"ActionPoint_{display_name}", None)
                            obj.empty_display_size = 0.3
                            obj.empty_display_type = 'SPHERE'
                            action_points_collection.objects.link(obj)
                            
                            for child in source_obj.children:
                                new_child = child.copy()
                                new_child.data = child.data.copy() if child.data else None
                                new_child.parent = obj
                                action_points_collection.objects.link(new_child)
                            
                            print(f"    Duplicated {model_name} for action point {name}")
                            action_point_count += 1
                    else:
                        # Find and load MDB file
                        mdb_path = find_mdb_file(model_name, game_path)
                        if mdb_path:
                            print(f"    Loading MDB for action point {name} (model: {model_name}): {os.path.basename(mdb_path)}")
                            imported_objects = load_mdb_model(
                                mdb_path, 
                                game_path, 
                                self.time_of_day,
                                import_speedtrees=True
                            )
                            
                            if imported_objects:
                                # Create a parent empty to hold all meshes
                                display_name = tag if tag and not tag.startswith("<invalid") else name
                                obj = bpy.data.objects.new(f"ActionPoint_{display_name}", None)
                                obj.empty_display_size = 0.3
                                obj.empty_display_type = 'SPHERE'
                                action_points_collection.objects.link(obj)
                                
                                loaded_mdbs[cache_key] = obj
                                
                                # Parent all imported objects to this empty
                                for imported_obj in imported_objects:
                                    for col in imported_obj.users_collection:
                                        col.objects.unlink(imported_obj)
                                    action_points_collection.objects.link(imported_obj)
                                    imported_obj.parent = obj
                                
                                action_point_count += 1
                                print(f"      Loaded {len(imported_objects)} meshes")
                            else:
                                print(f"      Failed to load MDB for {model_name}")
                        else:
                            print(f"      MDB file not found for model: {model_name}")
                
                if obj is None and self.create_placeholders:
                    # Create a distinctive placeholder for action points
                    bpy.ops.mesh.primitive_uv_sphere_add(radius=0.3, location=blender_pos)
                    placeholder_obj = bpy.context.active_object
                    placeholder_obj.name = f"ActionPoint_Placeholder_{model_name}"
                    
                    placeholder_obj.rotation_mode = 'QUATERNION'
                    placeholder_obj.rotation_quaternion = blender_q
                    
                    if "ActionPoint_Placeholder_Mat" not in bpy.data.materials:
                        mat = bpy.data.materials.new(name="ActionPoint_Placeholder_Mat")
                        mat.use_nodes = True
                        mat.node_tree.nodes["Principled BSDF"].inputs[0].default_value = (0.8, 0.2, 0.8, 0.5)  # Purple semi-transparent
                    else:
                        mat = bpy.data.materials["ActionPoint_Placeholder_Mat"]
                    
                    if placeholder_obj.data.materials:
                        placeholder_obj.data.materials[0] = mat
                    else:
                        placeholder_obj.data.materials.append(mat)
                    
                    for col in placeholder_obj.users_collection:
                        col.objects.unlink(placeholder_obj)
                    action_points_collection.objects.link(placeholder_obj)
                    
                    obj = placeholder_obj
                    action_point_placeholder_count += 1
                
                if obj:
                    obj.location = blender_pos
                    obj.rotation_mode = 'QUATERNION'
                    obj.rotation_quaternion = blender_q
                    
                    # Store action point properties
                    obj["Witcher_Type"] = "ActionPoint"
                    obj["Witcher_Name"] = name
                    obj["Witcher_Tag"] = tag or ""
                    obj["Witcher_Model"] = model_name
                    obj["Witcher_Actions"] = ", ".join(obj_data.get('actions', []))
                    
                    # Store other properties
                    for prop_name, prop_value in obj_data.get('properties', {}).items():
                        obj[f"Witcher_{prop_name}"] = str(prop_value)
                
                if (i + 1) % 50 == 0:
                    print(f"    Processed {i + 1} action points")
            
            print(f"\n{'='*60}")
            print(f"Importing Doors from GIT")
            print(f"{'='*60}")

            door_count = 0
            door_placeholder_count = 0

            for i, door_data in enumerate(doors):
                model_name = door_data.get('model_name', '')
                if not model_name:
                    print(f"    Skipping door {door_data.get('tag', 'unknown')} - no model name")
                    continue
                
                pos = door_data['position']
                orient = door_data['orientation']
                
                blender_pos, blender_q = witcher_to_blender_transform(pos, orient)
                
                print(f"\n    Door {i+1}: {door_data.get('tag', 'unknown')}")
                print(f"      Model: {model_name}")
                print(f"      Template: {door_data.get('template', '')}")
                print(f"      UniqueID: {door_data.get('unique_id', '')}")
                print(f"      Position: ({blender_pos[0]:.2f}, {blender_pos[1]:.2f}, {blender_pos[2]:.2f})")
                
                door_obj = None
                
                # Try to load door model
                if self.load_mdb_models:
                    cache_key = model_name
                    if cache_key in loaded_mdbs:
                        source_obj = loaded_mdbs[cache_key]
                        if source_obj:
                            door_obj = bpy.data.objects.new(f"Door_{model_name}_{door_data.get('unique_id', i)}", None)
                            door_obj.empty_display_size = 0.5
                            door_obj.empty_display_type = 'CUBE'
                            doors_collection.objects.link(door_obj)
                            
                            for child in source_obj.children:
                                new_child = child.copy()
                                new_child.data = child.data.copy() if child.data else None
                                new_child.parent = door_obj
                                doors_collection.objects.link(new_child)
                            
                            print(f"      Duplicated {model_name}")
                    else:
                        # Load new model
                        mdb_path = find_mdb_file(model_name, game_path)
                        if mdb_path:
                            imported_objects = load_mdb_model(
                                mdb_path, 
                                game_path, 
                                self.time_of_day,
                                import_speedtrees=False
                            )
                            
                            if imported_objects:
                                door_obj = bpy.data.objects.new(f"Door_{model_name}_{door_data.get('unique_id', i)}", None)
                                door_obj.empty_display_size = 0.5
                                door_obj.empty_display_type = 'CUBE'
                                doors_collection.objects.link(door_obj)
                                
                                # Store reference
                                loaded_mdbs[cache_key] = door_obj
                                
                                # Parent imported objects
                                for obj in imported_objects:
                                    for col in obj.users_collection:
                                        col.objects.unlink(obj)
                                    doors_collection.objects.link(obj)
                                    obj.parent = door_obj
                                    obj.location = (0, 0, 0)
                                    obj.rotation_mode = 'QUATERNION'
                                    obj.rotation_quaternion = (1, 0, 0, 0)
                                
                                print(f"      Loaded {len(imported_objects)} meshes")
                            else:
                                print(f"      Failed to load MDB for {model_name}")
                        else:
                            print(f"      MDB file not found for model: {model_name}")
                
                # Create placeholder if needed
                if door_obj is None and self.create_placeholders:
                    door_obj = create_door_placeholder(
                        blender_pos,
                        {
                            'x': orient[0],
                            'y': orient[1],
                            'z': orient[2],
                            'w': orient[3]
                        },
                        model_name,
                        doors_collection
                    )
                    door_placeholder_count += 1
                
                if door_obj:
                    door_obj.location = blender_pos
                    door_obj.rotation_mode = 'QUATERNION'
                    door_obj.rotation_quaternion = blender_q
                    
                    # Store door properties
                    door_obj["Witcher_Type"] = "Door"
                    door_obj["Witcher_Door_Model"] = model_name
                    door_obj["Witcher_Door_Template"] = door_data.get('template', '')
                    door_obj["Witcher_Door_Tag"] = door_data.get('tag', '')
                    door_obj["Witcher_Door_UniqueID"] = door_data.get('unique_id', '')
                    
                    # Store other properties
                    for prop_name, prop_value in door_data.get('properties', {}).items():
                        door_obj[f"Witcher_{prop_name}"] = str(prop_value)
                
                door_count += 1

            print(f"\n      Total doors imported from GIT: {door_count}")
                        
            # Load level mesh if requested and tileset is available
            if self.load_level_mesh and tileset:
                print(f"\n{'='*60}")
                print(f"Loading level mesh: {tileset}")
                print(f"{'='*60}")
                
                mdb_path = find_mdb_file(tileset, game_path)
                if mdb_path:
                    print(f"    Found level MDB: {os.path.basename(mdb_path)}")
                    imported_objects = load_mdb_model(
                        mdb_path, 
                        game_path, 
                        self.time_of_day,
                        import_speedtrees=self.import_speedtrees
                    )
                    
                    if imported_objects:
                        print(f"      Loaded {len(imported_objects)} meshes for level")

                else:
                    print(f"      Level MDB not found: {tileset}")
                    if self.create_placeholders:
                        # Create a large placeholder for the level
                        bpy.ops.mesh.primitive_cube_add(size=10.0, location=(0, 0, 0))
                        level_placeholder = bpy.context.active_object
                        level_placeholder.name = f"Level_Placeholder_{tileset}"
                        
                        if "Level_Placeholder_Mat" not in bpy.data.materials:
                            mat = bpy.data.materials.new(name="Level_Placeholder_Mat")
                            mat.use_nodes = True
                            mat.node_tree.nodes["Principled BSDF"].inputs[0].default_value = (0.0, 1.0, 0.0, 0.3)  # Green semi-transparent
                        else:
                            mat = bpy.data.materials["Level_Placeholder_Mat"]
                        
                        if level_placeholder.data.materials:
                            level_placeholder.data.materials[0] = mat
                        else:
                            level_placeholder.data.materials.append(mat)
                        
                        for col in level_placeholder.users_collection:
                            col.objects.unlink(level_placeholder)
                        level_collection.objects.link(level_placeholder)
            else:
                print(f"      No level mesh to load (tileset not specified)")
            
            context.view_layer.active_layer_collection = layer_collection
            cleanup_empty_collections()
            
            print(f"\n{'='*60}")
            print(f"Import Summary for area '{area_name}':")
            if localized_name:
                print(f"  Localized name: {localized_name} (Language: {LANGUAGE_NAMES.get(selected_lang, 'Unknown')})")
            if tileset:
                print(f"  Tileset: {tileset}")
            print(f"  Total placeables found: {len([o for o in objects if o.get('list_type') == 'Placeable'])}")
            print(f"  Total action points found: {len([o for o in objects if o.get('list_type') == 'ActionPoint'])}")
            print(f"  FX objects skipped: {fx_count}")
            print(f"  Non-FX placeables: {len([o for o in filtered_objects if o.get('list_type') == 'Placeable'])}")
            print(f"  Placeable models loaded: {mdb_count}")
            print(f"  Placeable placeholders created: {placeholder_count}")
            print(f"  Action points imported: {action_point_count}")
            print(f"  Action point placeholders: {action_point_placeholder_count}")
            print(f"  Doors imported: {door_count}")
            print(f"  Door placeholders: {door_placeholder_count}")
            if self.load_level_mesh and tileset:
                print(f"  Level mesh: {tileset}")
            print(f"{'='*60}")
            
            self.report({'INFO'}, f"Imported {len(filtered_objects)} placeables, {door_count} doors, and level mesh from area '{area_name}'")
            
            # Reset state for next import
            self.reset_state()
            
            return {'FINISHED'}


# ============
# Registration
# ============

def menu_func_import(self, context):
    self.layout.operator(IMPORT_WITCHER_MOD_OT_operator.bl_idname, text="Witcher MOD (.mod, .adv)")


classes = [
    WitcherImporterPreferences,
    AreaInfo,
    AREA_UL_areas,
    IMPORT_WITCHER_MOD_OT_operator,
]

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(menu_func_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(menu_func_import)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()