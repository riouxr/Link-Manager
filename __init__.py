bl_info = {
    "name": "Link Manager",
    "author": "Your Name",
    "version": (2, 2, 1),  # Fixed library override restoration
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Link Manager",
    "description": "List linked files with expand toggle, relocate, reload, delete and add link buttons",
    "category": "Object",
}

import bpy
import os
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ImportHelper
from bpy.props import StringProperty

# ============================================================================
# CONSTANTS
# ============================================================================

LO_SUFFIX = "_Lo.blend"
DATA_BLOCK_TYPES = ('lights', 'materials', 'cameras', 'meshes', 'armatures', 
                    'curves', 'lattices', 'metaballs', 'texts', 'grease_pencils', 'images')
OBJECT_DATA_TYPES = ('meshes', 'armatures', 'curves', 'lattices', 'metaballs')
METADATA_KEYS = ('options', 'collection_instances', 'type', 'transforms', 'object_collections')

# ============================================================================
# GLOBAL STATE
# ============================================================================

library_order = []
expanded_states = {}
link_active_states = {}
linked_elements = {}
resolution_status = {}
ephemerally_loaded_libraries = set()
ephemeral_hidden_libraries = set()
_RENDER_SWAPS = {}

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def normalize_filepath(filepath):
    """Return Blender-style forward-slash path (relative if prefs allow)."""
    abs_path = bpy.path.abspath(filepath)
    if bpy.context.preferences.filepaths.use_relative_paths:
        try:
            rel = bpy.path.relpath(abs_path)
            return rel.replace("\\", "/")
        except ValueError:
            pass
    return abs_path.replace("\\", "/")


def safe_library(id_block):
    """Return item.library or None if the pointer is already invalid."""
    try:
        return id_block.library
    except ReferenceError:
        return None


def force_viewport_refresh():
    """Redraw every 3D viewport and update view layer in every Blender window."""
    bpy.context.view_layer.update()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    region.tag_redraw()


def reload_library(lib):
    """Version-safe wrapper for Library.reload()."""
    try:
        lib.reload()
    except RuntimeError:
        lib.reload()


def find_library_by_filepath(filepath):
    """Find a library by normalized filepath."""
    normalized = normalize_filepath(filepath)
    return next((lib for lib in bpy.data.libraries 
                 if normalize_filepath(lib.filepath) == normalized), None)

# ============================================================================
# LOW/HIGH RESOLUTION HELPERS
# ============================================================================

def is_lo_file(path):
    """Check if path ends with the low-res suffix."""
    return normalize_filepath(path).endswith(LO_SUFFIX)


def get_hi_res_path(path):
    """Convert a low-res path to its hi-res counterpart."""
    normalized = normalize_filepath(path)
    if is_lo_file(normalized):
        return normalized[:-len(LO_SUFFIX)] + ".blend"
    return normalized


def lib_base(path):
    """Strip '.blend' and any low-res suffix to obtain a library base key."""
    normalized = normalize_filepath(path)
    if normalized.endswith(LO_SUFFIX):
        return normalized[:-len(LO_SUFFIX)]
    if normalized.lower().endswith(".blend"):
        return normalized[:-6]
    return normalized


def strip_lo_suffix(name):
    """Remove low-res suffix from an asset name."""
    for suffix in ("_Lo", "_lo", "_Low", "_low"):
        if name.endswith(suffix):
            return name[:-len(suffix)]
    return name

# ============================================================================
# LINKED ITEM CAPTURE
# ============================================================================

def get_all_library_objects_with_state(library, context):
    """
    Capture ALL objects that use this library (including Alt+D duplicates)
    with their complete state for restoration.
    """
    try:
        lib_fp_norm = normalize_filepath(library.filepath)
    except ReferenceError:
        return {}
    
    result = {
        'all_objects': [],  # List of dicts with full object state
        'filepath': lib_fp_norm
    }
    
    # Find ALL objects in the scene that reference this library
    for obj in context.scene.objects:
        obj_lib = safe_library(obj)
        data_lib = safe_library(obj.data) if obj.data else None
        
        # Debug EVERY object we examine
        print(f"  EXAMINING {obj.name}: obj.library={obj_lib is not None}, data.library={data_lib is not None}")
        
        # Check if object or its data is from this library
        is_from_library = (
            (obj_lib and normalize_filepath(obj_lib.filepath) == lib_fp_norm) or
            (data_lib and normalize_filepath(data_lib.filepath) == lib_fp_norm)
        )
        
        if is_from_library:
            # Determine duplicate/override status
            has_override = hasattr(obj, 'override_library') and obj.override_library
            is_duplicate = (obj_lib is None and data_lib is not None and not has_override)
            
            # Debug captured object
            obj_lib_str = "HAS_LIB" if obj_lib else "NO_LIB"
            data_lib_str = "HAS_DATA_LIB" if data_lib else "NO_DATA_LIB"
            if has_override:
                dup_str = "OVERRIDE"
            elif is_duplicate:
                dup_str = "DUP"
            else:
                dup_str = "BASE"
            print(f"    -> CAPTURED as {dup_str}: {obj_lib_str}, {data_lib_str}")
            
            obj_state = {
                'name': obj.name,
                'base_name': obj.name.split('.')[0],  # Name without .001, .002, etc
                'is_duplicate': is_duplicate,  # True if Alt+D duplicate
                'has_override': has_override,  # True if library override
                'collections': [c.name for c in obj.users_collection],
                'location': list(obj.location),
                'rotation_mode': obj.rotation_mode,
                'hide_viewport': obj.hide_viewport,
                'hide_render': obj.hide_render,
                'hide_select': obj.hide_select,
            }
            
            # Store rotation based on mode
            if obj.rotation_mode == 'QUATERNION':
                obj_state['rotation'] = list(obj.rotation_quaternion)
            elif obj.rotation_mode == 'AXIS_ANGLE':
                obj_state['rotation'] = list(obj.rotation_axis_angle)
            else:
                obj_state['rotation'] = list(obj.rotation_euler)
            
            obj_state['scale'] = list(obj.scale)
            
            result['all_objects'].append(obj_state)
    
    return result


def get_linked_item_names(library, context):
    """
    Capture all linked items from a library including collections, objects,
    and data blocks, along with their configuration options.
    """
    try:
        lib_fp_norm = normalize_filepath(library.filepath)
    except ReferenceError:
        return {}

    result = {}
    collections = []
    collection_instances = {}
    objects = []
    transforms = {}

    # Determine if path is relative
    abs_fp = bpy.path.abspath(library.filepath)
    is_relative = False
    if context.preferences.filepaths.use_relative_paths:
        try:
            rel_fp = bpy.path.relpath(abs_fp)
            is_relative = (library.filepath == rel_fp and library.filepath != abs_fp)
        except ValueError:
            pass

    options = {
        "relative_path": is_relative,
        "active_collection": True,
        "instance_collections": False,
        "instance_object_data": False
    }

    active_col = context.view_layer.active_layer_collection.collection

    # Process collections
    for coll in bpy.data.collections:
        lib = safe_library(coll)
        if lib and normalize_filepath(lib.filepath) == lib_fp_norm:
            collections.append(coll.name)
            
            # Check if collection is instanced
            instance_empty = _find_collection_instance(coll, lib_fp_norm)
            if instance_empty:
                collection_instances[coll.name] = instance_empty.name
                options["instance_collections"] = True
                instance_empty.rotation_mode = 'QUATERNION'
                transforms[coll.name] = {
                    'location': list(instance_empty.location),
                    'rotation': list(instance_empty.rotation_quaternion),
                    'scale': list(instance_empty.scale)
                }
            elif coll.name not in [c.name for c in active_col.children]:
                options["instance_collections"] = False

    # Process objects
    for obj in bpy.data.objects:
        lib = safe_library(obj)
        if not lib or normalize_filepath(lib.filepath) != lib_fp_norm:
            continue
            
        # Handle collection instances
        if obj.type == 'EMPTY' and obj.instance_collection:
            coll_name = obj.instance_collection.name
            if coll_name not in collections:
                collections.append(coll_name)
            if coll_name not in collection_instances:
                collection_instances[coll_name] = obj.name
            options["instance_collections"] = True
            obj.rotation_mode = 'QUATERNION'
            transforms[coll_name] = {
                'location': list(obj.location),
                'rotation': list(obj.rotation_quaternion),
                'scale': list(obj.scale)
            }
        # Handle regular objects
        else:
            obj_collection_names = [c.name for c in obj.users_collection]
            is_in_active = (obj.name in [o.name for o in active_col.objects] or 
                           any(c in collections for c in obj_collection_names))
            
            if obj.name not in collection_instances.values() and is_in_active:
                # Check if object data is also linked
                if obj.data and safe_library(obj.data):
                    data_lib = safe_library(obj.data)
                    if normalize_filepath(data_lib.filepath) == lib_fp_norm:
                        options["instance_object_data"] = True
                
                if obj.name not in objects:
                    objects.append(obj.name)

    # Return early if we found collections
    if collections:
        result['type'] = 'collections'
        result['collections'] = collections
        result['collection_instances'] = collection_instances
        result['options'] = options
        result['transforms'] = transforms
        return result
    
    # Return early if we found objects
    if objects:
        result['type'] = 'objects'
        result['objects'] = objects
        result['options'] = options
        
        # Store which collections each object is in
        result['object_collections'] = {}
        for obj_name in objects:
            obj = bpy.data.objects.get(obj_name)
            if obj:
                result['object_collections'][obj_name] = [c.name for c in obj.users_collection]
        
        # Capture data blocks if instance_object_data is True
        if options.get('instance_object_data'):
            _capture_data_blocks(result, lib_fp_norm, DATA_BLOCK_TYPES)
        
        return result

    # Fallback: capture all data blocks
    objects_found = _find_objects_from_library(lib_fp_norm)
    _capture_data_blocks(result, lib_fp_norm, DATA_BLOCK_TYPES)
    
    # Store objects if found, AND their collection memberships
    if objects_found:
        result['objects'] = objects_found
        # Store which collections each object is in
        result['object_collections'] = {}
        for obj_name in objects_found:
            obj = bpy.data.objects.get(obj_name)
            if obj:
                result['object_collections'][obj_name] = [c.name for c in obj.users_collection]
    
    result['type'] = 'other'
    result['options'] = options
    return result


def _find_collection_instance(collection, lib_fp_norm):
    """Find the empty object that instances a given collection."""
    for obj in bpy.data.objects:
        if (obj.type == 'EMPTY' and 
            obj.instance_collection == collection):
            return obj
    return None


def _find_objects_from_library(lib_fp_norm):
    """Find all objects from a specific library."""
    objects = []
    for obj in bpy.data.objects:
        lib = safe_library(obj)
        if lib and normalize_filepath(lib.filepath) == lib_fp_norm:
            objects.append(obj.name)
    return objects


def _capture_data_blocks(result, lib_fp_norm, data_types):
    """Capture data blocks of specified types from a library."""
    for dt in data_types:
        names = []
        for item in getattr(bpy.data, dt):
            lib = safe_library(item)
            if lib and normalize_filepath(lib.filepath) == lib_fp_norm:
                names.append(item.name)
                if dt in OBJECT_DATA_TYPES:
                    result.setdefault('options', {})["instance_object_data"] = True
        if names:
            result[dt] = names

# ============================================================================
# HI-RES LOADER (HIDDEN)
# ============================================================================

def load_highres_hidden(lo_fp):
    """
    Load high-res meshes/collections in the background for low-res libraries.
    Used for render-time resolution swapping.
    """
    hi_fp = resolution_status.get(lo_fp, {}).get("high_path")
    if not hi_fp or not os.path.exists(bpy.path.abspath(hi_fp)):
        return False

    need_meshes = set()
    need_colls = set()
    
    for obj in bpy.data.objects:
        lib = safe_library(obj)
        if not lib or normalize_filepath(lib.filepath) != lo_fp:
            continue
            
        if obj.type == 'MESH':
            need_meshes.add(strip_lo_suffix(obj.data.name))
        elif obj.type == 'EMPTY' and obj.instance_collection:
            need_colls.add(strip_lo_suffix(obj.instance_collection.name))

    if not need_meshes and not need_colls:
        return False

    try:
        with bpy.data.libraries.load(hi_fp, link=True) as (src, dst):
            dst.meshes = [m for m in src.meshes if strip_lo_suffix(m) in need_meshes]
            dst.collections = [c for c in src.collections if strip_lo_suffix(c) in need_colls]
    except Exception:
        return False

    lib = find_library_by_filepath(hi_fp)
    if lib:
        ephemerally_loaded_libraries.add(lib)
        ephemeral_hidden_libraries.add(hi_fp)
        return True
    return False

# ============================================================================
# EVENT HANDLERS
# ============================================================================

@persistent
def linkeditor_load_post(dummy):
    """Clear all cached link-editor state when a new .blend is loaded."""
    library_order.clear()
    expanded_states.clear()
    link_active_states.clear()
    linked_elements.clear()
    resolution_status.clear()
    ephemerally_loaded_libraries.clear()
    ephemeral_hidden_libraries.clear()
    _RENDER_SWAPS.clear()


@persistent
def monitor_libraries(dummy):
    """Update linked_elements with options for newly linked libraries."""
    # Disabled - not needed with new comprehensive state capture
    pass


@persistent
def prepare_render(scene, _):
    """Swap low-res libraries to high-res before rendering if enabled."""
    for fp, rs in resolution_status.items():
        if rs.get("status") != "low" or not rs.get("high_res_for_render"):
            continue
            
        hi_fp = rs["high_path"]
        lib = find_library_by_filepath(fp)
        
        if not lib or normalize_filepath(lib.filepath) == hi_fp:
            continue
            
        _RENDER_SWAPS[id(lib)] = lib.filepath
        lib.filepath = hi_fp
        reload_library(lib)
    
    bpy.context.view_layer.update()


@persistent
def restore_render(scene, _):
    """Restore low-res libraries after rendering."""
    for lib in bpy.data.libraries:
        orig_low = _RENDER_SWAPS.pop(id(lib), None)
        if not orig_low or normalize_filepath(lib.filepath) == orig_low:
            continue
            
        lib.filepath = orig_low
        reload_library(lib)
    
    bpy.context.view_layer.update()
    force_viewport_refresh()

# ============================================================================
# OPERATORS
# ============================================================================

class LINKEDITOR_OT_toggle_expand(bpy.types.Operator):
    """Toggle expanded state to show/hide filepath."""
    bl_idname = "linkeditor.toggle_expand"
    bl_label = "Toggle Expand"
    filepath: StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        expanded_states[fp] = not expanded_states.get(fp, False)
        return {'FINISHED'}


class LINKEDITOR_OT_render_resolution(bpy.types.Operator):
    """Toggle whether this low-res library is swapped to hi-res at render time."""
    bl_idname = "linkeditor.render_resolution"
    bl_label = "Toggle Render Resolution"
    filepath: StringProperty()

    def execute(self, context):
        lo_fp = normalize_filepath(self.filepath)
        
        # Only works on low-res files
        if (resolution_status.get(lo_fp, {}).get("status") != "low" and 
            not is_lo_file(lo_fp)):
            self.report({'WARNING'}, "Works only on low-res files.")
            return {'CANCELLED'}
        
        # Initialize or toggle the setting
        rs = resolution_status.setdefault(lo_fp, {
            "status": "low",
            "low_path": lo_fp,
            "high_path": get_hi_res_path(lo_fp),
            "high_res_for_render": False,
        })
        rs["high_res_for_render"] = not rs["high_res_for_render"]
        
        force_viewport_refresh()
        state = "ON" if rs["high_res_for_render"] else "OFF"
        self.report({'INFO'}, f"Hi-res render {state}.")
        return {'FINISHED'}


class LINKEDITOR_OT_load_and_unload(bpy.types.Operator):
    """Unload a library if it's loaded, or re-link it if it was unloaded."""
    bl_idname = "linkeditor.load_and_unload"
    bl_label = "Load/Unload Linked File"
    filepath: StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        lib = find_library_by_filepath(fp)

        # Unload library
        if lib:
            self._unload_library(context, lib, fp)
            return {'FINISHED'}
        
        # Reload library
        elif fp in linked_elements:
            self._reload_library(context, fp)
            return {'FINISHED'}

        self.report({'WARNING'}, "No library to unload or reload")
        return {'CANCELLED'}

    def _unload_library(self, context, lib, fp):
        """Unload a library and save complete state of ALL objects."""
        
        # Capture NEW comprehensive format with ALL objects and states
        full_state = get_all_library_objects_with_state(lib, context)
        linked_elements[f"{fp}_full_state"] = full_state
        
        # Capture what data blocks need to be reloaded (objects, meshes, materials, etc)
        # This must happen BEFORE we remove the library
        data_to_reload = {}
        
        # Get list of unique base object names (without .001, .002, etc)
        base_names = list(set([obj['base_name'] for obj in full_state['all_objects']]))
        data_to_reload['objects'] = base_names
        
        # Also capture meshes, materials, and images that these objects use
        for obj in context.scene.objects:
            obj_lib = safe_library(obj)
            data_lib = safe_library(obj.data) if obj.data else None
            
            if ((obj_lib and normalize_filepath(obj_lib.filepath) == fp) or
                (data_lib and normalize_filepath(data_lib.filepath) == fp)):
                
                # Capture mesh
                if obj.data and hasattr(obj.data, 'name'):
                    data_to_reload.setdefault('meshes', []).append(obj.data.name)
                
                # Capture materials
                if hasattr(obj, 'material_slots'):
                    for slot in obj.material_slots:
                        if slot.material:
                            mat_lib = safe_library(slot.material)
                            if mat_lib and normalize_filepath(mat_lib.filepath) == fp:
                                data_to_reload.setdefault('materials', []).append(slot.material.name)
        
        # Remove duplicates
        for key in data_to_reload:
            if isinstance(data_to_reload[key], list):
                data_to_reload[key] = list(set(data_to_reload[key]))
        
        linked_elements[fp] = data_to_reload
        
        print(f"\n=== UNLOADING: {os.path.basename(fp)} ===")
        print(f"Found {len(full_state['all_objects'])} total objects (including duplicates)")
        print(f"Data to reload: {list(data_to_reload.keys())}")
        
        for obj_state in full_state['all_objects']:
            dup_marker = " [DUPLICATE]" if obj_state['is_duplicate'] else " [BASE]"
            print(f"  {obj_state['name']}{dup_marker} in collections: {obj_state['collections']}")
        
        # Remove ALL objects from scene
        removed_count = 0
        for obj in list(context.scene.objects):
            obj_lib = safe_library(obj)
            data_lib = safe_library(obj.data) if obj.data else None
            
            if ((obj_lib and normalize_filepath(obj_lib.filepath) == fp) or
                (data_lib and normalize_filepath(data_lib.filepath) == fp)):
                
                # Unlink from all collections
                for coll in list(obj.users_collection):
                    coll.objects.unlink(obj)
                removed_count += 1
        
        print(f"Removed {removed_count} objects from scene")
        
        # Remove the library
        bpy.data.libraries.remove(lib)
        link_active_states[fp] = False
        force_viewport_refresh()
        print(f"=== END UNLOAD ===\n")
        self.report({'INFO'}, f"Unloaded: {os.path.basename(fp)}")

    def _reload_library(self, context, fp):
        """Reload library and restore ALL objects with their complete state."""
        print(f"\n=== RELOADING: {os.path.basename(fp)} ===")
        
        # Check if we have the new full state format
        full_state_key = f"{fp}_full_state"
        if full_state_key not in linked_elements:
            print("WARNING: No full state found, falling back to old method")
            # Fallback to old reload method
            return self._reload_library_old_method(context, fp)
        
        full_state = linked_elements[full_state_key]
        items = linked_elements.get(fp, {})  # Old format for data loading
        
        print(f"Full state has {len(full_state.get('all_objects', []))} objects")
        print(f"Items dict has these keys: {list(items.keys())}")
        print(f"Restoring {len(full_state['all_objects'])} objects...")
        
        # Load base data from library
        print("Loading data from library...")
        with bpy.data.libraries.load(fp, link=True) as (src, dst):
            for dt, names in items.items():
                if dt in METADATA_KEYS or not isinstance(names, list):
                    continue
                if hasattr(src, dt) and hasattr(dst, dt):
                    available = getattr(src, dt, [])
                    to_load = [e for e in available if e in names]
                    if to_load:
                        setattr(dst, dt, to_load)
                        print(f"  Loaded {len(to_load)} {dt}: {to_load}")
        
        # Debug: Check what objects are actually in bpy.data.objects now
        print("\nObjects in bpy.data.objects after loading:")
        for obj_name in [o['base_name'] for o in full_state['all_objects']]:
            obj = bpy.data.objects.get(obj_name)
            if obj:
                print(f"  Found: {obj.name}, has .library: {obj.library is not None}")
            else:
                print(f"  NOT FOUND: {obj_name}")
        
        # Now restore ALL objects exactly as they were
        # Track which objects we actually restore
        restored_objects = set()
        
        # Process base objects first, then overrides, then duplicates
        # This prevents name collision issues
        base_objects_to_restore = [obj for obj in full_state['all_objects'] 
                                   if not obj['is_duplicate'] and not obj.get('has_override', False)]
        override_objects_to_restore = [obj for obj in full_state['all_objects'] 
                                      if obj.get('has_override', False)]
        duplicate_objects_to_restore = [obj for obj in full_state['all_objects'] 
                                        if obj['is_duplicate']]
        
        print(f"\nProcessing {len(base_objects_to_restore)} base objects, {len(override_objects_to_restore)} overrides, and {len(duplicate_objects_to_restore)} duplicates...")
        
        # STEP 1: Restore all base objects
        for obj_state in base_objects_to_restore:
            obj_name = obj_state['name']
            base_name = obj_state['base_name']
            
            # Base objects should already be loaded from library
            if base_name not in bpy.data.objects:
                print(f"  ERROR: Base object '{base_name}' not found after loading")
                continue
            
            new_obj = bpy.data.objects[base_name]
            obj_lib_status = "LINKED" if safe_library(new_obj) else "LOCAL"
            data_lib_status = "LINKED" if (new_obj.data and safe_library(new_obj.data)) else "LOCAL"
            print(f"  Restoring base: {new_obj.name} (obj={obj_lib_status}, data={data_lib_status})")
            
            # Track that we used this object
            restored_objects.add(new_obj.name)
            
            # Restore transforms
            new_obj.location = obj_state['location']
            new_obj.rotation_mode = obj_state['rotation_mode']
            
            if obj_state['rotation_mode'] == 'QUATERNION':
                new_obj.rotation_quaternion = obj_state['rotation']
            elif obj_state['rotation_mode'] == 'AXIS_ANGLE':
                new_obj.rotation_axis_angle = obj_state['rotation']
            else:
                new_obj.rotation_euler = obj_state['rotation']
            
            new_obj.scale = obj_state['scale']
            
            # Restore visibility states
            new_obj.hide_viewport = obj_state['hide_viewport']
            new_obj.hide_render = obj_state['hide_render']
            new_obj.hide_select = obj_state['hide_select']
            
            # Link to collections
            for coll_name in obj_state['collections']:
                coll = bpy.data.collections.get(coll_name)
                if coll:
                    if new_obj.name not in coll.objects:
                        coll.objects.link(new_obj)
                        print(f"    -> Linked to collection '{coll_name}'")
                else:
                    print(f"    -> WARNING: Collection '{coll_name}' not found")
        
        # STEP 2: Create library overrides
        for obj_state in override_objects_to_restore:
            obj_name = obj_state['name']
            base_name = obj_state['base_name']
            
            # Find the base object to override
            base_obj = bpy.data.objects.get(base_name)
            if not base_obj:
                print(f"  ERROR: Base '{base_name}' not found for override '{obj_name}'")
                continue
            
            # Create library override
            try:
                new_obj = base_obj.override_create()
                print(f"  Creating override: {new_obj.name} (from {base_name})")
                
                # Link to first collection immediately
                if obj_state['collections']:
                    first_coll = bpy.data.collections.get(obj_state['collections'][0])
                    if first_coll:
                        first_coll.objects.link(new_obj)
                
                # Track that we used this object
                restored_objects.add(new_obj.name)
                
                # Restore transforms
                new_obj.location = obj_state['location']
                new_obj.rotation_mode = obj_state['rotation_mode']
                
                if obj_state['rotation_mode'] == 'QUATERNION':
                    new_obj.rotation_quaternion = obj_state['rotation']
                elif obj_state['rotation_mode'] == 'AXIS_ANGLE':
                    new_obj.rotation_axis_angle = obj_state['rotation']
                else:
                    new_obj.rotation_euler = obj_state['rotation']
                
                new_obj.scale = obj_state['scale']
                
                # Restore visibility states
                new_obj.hide_viewport = obj_state['hide_viewport']
                new_obj.hide_render = obj_state['hide_render']
                new_obj.hide_select = obj_state['hide_select']
                
                # Link to remaining collections
                for coll_name in obj_state['collections']:
                    coll = bpy.data.collections.get(coll_name)
                    if coll:
                        if new_obj.name not in coll.objects:
                            coll.objects.link(new_obj)
                            print(f"    -> Linked to collection '{coll_name}'")
                            
            except Exception as e:
                print(f"  ERROR: Could not create override for '{obj_name}': {e}")
        
        # STEP 3: Create all duplicates
        for obj_state in duplicate_objects_to_restore:
            obj_name = obj_state['name']
            base_name = obj_state['base_name']
            
            # Special case: if duplicate has same name as base, the loaded object IS the duplicate
            if obj_name == base_name and obj_name in bpy.data.objects:
                new_obj = bpy.data.objects[obj_name]
                print(f"  Using loaded object as duplicate: {new_obj.name} (no copy needed)")
            else:
                # Find the base object
                base_obj = bpy.data.objects.get(base_name)
                if not base_obj:
                    print(f"  ERROR: Base '{base_name}' not found for duplicate '{obj_name}'")
                    continue
                
                # Create Alt+D duplicate
                new_obj = base_obj.copy()
                
                # Link to first collection immediately so object is registered
                if obj_state['collections']:
                    first_coll = bpy.data.collections.get(obj_state['collections'][0])
                    if first_coll:
                        first_coll.objects.link(new_obj)
                
                # Set the name to match the original duplicate name
                new_obj.name = obj_name
                print(f"  Creating duplicate: {new_obj.name} (from {base_name})")
            
            # Track that we used this object
            restored_objects.add(new_obj.name)
            
            # Restore transforms
            new_obj.location = obj_state['location']
            new_obj.rotation_mode = obj_state['rotation_mode']
            
            if obj_state['rotation_mode'] == 'QUATERNION':
                new_obj.rotation_quaternion = obj_state['rotation']
            elif obj_state['rotation_mode'] == 'AXIS_ANGLE':
                new_obj.rotation_axis_angle = obj_state['rotation']
            else:
                new_obj.rotation_euler = obj_state['rotation']
            
            new_obj.scale = obj_state['scale']
            
            # Restore visibility states
            new_obj.hide_viewport = obj_state['hide_viewport']
            new_obj.hide_render = obj_state['hide_render']
            new_obj.hide_select = obj_state['hide_select']
            
            # Link to remaining collections
            for coll_name in obj_state['collections']:
                coll = bpy.data.collections.get(coll_name)
                if coll:
                    if new_obj.name not in coll.objects:
                        coll.objects.link(new_obj)
                        print(f"    -> Linked to collection '{coll_name}'")
        
        
        # Summary of what we restored
        print(f"\nRESTORED SUMMARY:")
        total_expected = len(base_objects_to_restore) + len(override_objects_to_restore) + len(duplicate_objects_to_restore)
        print(f"  Expected: {len(base_objects_to_restore)} base + {len(override_objects_to_restore)} overrides + {len(duplicate_objects_to_restore)} duplicates = {total_expected} total")
        print(f"  Tracked in restored_objects: {len(restored_objects)} objects")
        print(f"  Names: {sorted(restored_objects)}")
        
        # Clean up: Remove any base objects that weren't actually restored
        print("\nCleaning up unused base objects...")
        for obj in list(bpy.data.objects):
            obj_lib = safe_library(obj)
            if obj_lib:
                try:
                    obj_lib_path = normalize_filepath(obj_lib.filepath)
                    if obj_lib_path == fp and obj.name not in restored_objects:
                        print(f"  Removing unused: {obj.name}")
                        bpy.data.objects.remove(obj, do_unlink=True)
                except:
                    pass
        
        link_active_states[fp] = True
        force_viewport_refresh()
        print(f"=== END RELOAD ===\n")
        self.report({'INFO'}, f"Reloaded: {os.path.basename(fp)}")
    
    def _reload_library_old_method(self, context, fp):
        """Old reload method for backward compatibility."""
        items = linked_elements.get(fp)
        if not items:
            self.report({'ERROR'}, f"No stored data for {os.path.basename(fp)}")
            return
        
        options = items.get('options', {}).copy()
        transforms = items.get('transforms', {})
        previous_instances = items.get('collection_instances', {})

        # Load data from file
        with bpy.data.libraries.load(fp, link=True) as (src, dst):
            for dt, names in items.items():
                if dt in METADATA_KEYS or not isinstance(names, list):
                    continue
                if hasattr(src, dt) and hasattr(dst, dt):
                    available = getattr(src, dt, [])
                    setattr(dst, dt, [e for e in available if e in names])

        active_col = context.view_layer.active_layer_collection.collection
        
        # Re-link collections or objects
        if items['type'] == 'collections':
            self._reload_collections(fp, items, active_col, options, 
                                    transforms, previous_instances)
        else:
            self._reload_objects(fp, items, active_col)

        # Restore relative path if needed
        lib = find_library_by_filepath(fp)
        if lib and options.get('relative_path'):
            try:
                lib.filepath = bpy.path.relpath(bpy.path.abspath(fp))
            except ValueError:
                pass

        link_active_states[fp] = True
        force_viewport_refresh()
        self.report({'INFO'}, f"Reloaded: {os.path.basename(fp)}")

    def _reload_collections(self, fp, items, active_col, options, 
                           transforms, previous_instances):
        """Re-link collection instances."""
        for coll_name in items['collections']:
            coll = next((c for c in bpy.data.collections 
                        if c.name == coll_name and safe_library(c) and
                        normalize_filepath(c.library.filepath) == fp), None)
            
            if not coll:
                continue
                
            if options.get('instance_collections'):
                empty_name = previous_instances.get(coll_name, f"{coll_name}_instance")
                count = 1
                while empty_name in bpy.data.objects:
                    empty_name = f"{coll_name}_instance.{count:03d}"
                    count += 1
                
                empty = bpy.data.objects.new(name=empty_name, object_data=None)
                empty.instance_type = 'COLLECTION'
                empty.instance_collection = coll
                empty.rotation_mode = 'QUATERNION'
                active_col.objects.link(empty)
                
                # Restore transforms
                tr = transforms.get(coll_name, {})
                empty.location = tr.get('location', (0, 0, 0))
                empty.rotation_quaternion = tr.get('rotation', (1, 0, 0, 0))
                empty.scale = tr.get('scale', (1, 1, 1))

    def _reload_objects(self, fp, items, active_col):
        """Re-link objects to their original collections."""
        linked_count = 0
        object_collections = items.get('object_collections', {})
        
        for obj_name in items.get('objects', []):
            obj = bpy.data.objects.get(obj_name)
            if obj:
                obj_lib = safe_library(obj)
                if obj_lib:
                    print(f"  Object '{obj_name}': library={normalize_filepath(obj_lib.filepath)}, target={fp}, match={normalize_filepath(obj_lib.filepath) == fp}")
                else:
                    print(f"  Object '{obj_name}': NO LIBRARY (local object?)")
                    
                if obj_lib and normalize_filepath(obj_lib.filepath) == fp:
                    # Get the collections this object should be in
                    target_collections = object_collections.get(obj_name, [])
                    
                    if target_collections:
                        # Link to original collections
                        for coll_name in target_collections:
                            coll = bpy.data.collections.get(coll_name)
                            if coll and obj.name not in coll.objects:
                                coll.objects.link(obj)
                                linked_count += 1
                                print(f"    -> Linked to collection '{coll_name}'")
                            elif coll:
                                print(f"    -> Already in collection '{coll_name}'")
                            else:
                                print(f"    -> WARNING: Collection '{coll_name}' not found!")
                    else:
                        # Fallback: link to active collection if no collection info stored
                        if obj.name not in active_col.objects:
                            active_col.objects.link(obj)
                            linked_count += 1
                            print(f"    -> Linked to active collection (no stored collection info)")
                        else:
                            print(f"    -> Already in active collection")
            else:
                print(f"  Object '{obj_name}': NOT FOUND in bpy.data.objects")
        print(f"Total objects linked: {linked_count}")


class LINKEDITOR_OT_reload(bpy.types.Operator):
    """Reload a linked .blend, preserving only the previously visible items."""
    bl_idname = "linkeditor.reload"
    bl_label = "Reload Linked File"
    filepath: StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        lib = find_library_by_filepath(fp)

        # Capture current state using the new method
        if lib:
            # Use the new full state capture
            full_state = get_all_library_objects_with_state(lib, context)
            linked_elements[f"{fp}_full_state"] = full_state
            
            # Also capture old format for data loading
            linked_elements[fp] = get_linked_item_names(lib, context)
            
            # Remove collection instances if needed
            if linked_elements[fp]['type'] == 'collections':
                self._remove_collection_instances(context, fp, linked_elements[fp])
            
            # Remove all objects from scene
            for obj in list(context.scene.objects):
                obj_lib = safe_library(obj)
                data_lib = safe_library(obj.data) if obj.data else None
                
                if ((obj_lib and normalize_filepath(obj_lib.filepath) == fp) or
                    (data_lib and normalize_filepath(data_lib.filepath) == fp)):
                    for coll in list(obj.users_collection):
                        coll.objects.unlink(obj)
            
            bpy.data.libraries.remove(lib)

        # Check if we have items to reload
        full_state_key = f"{fp}_full_state"
        if full_state_key not in linked_elements:
            self.report({'WARNING'}, "No items found to reload")
            return {'CANCELLED'}

        items = linked_elements.get(fp, {})
        full_state = linked_elements[full_state_key]
        
        # Load data from library
        with bpy.data.libraries.load(fp, link=True) as (src, dst):
            for dt, names in items.items():
                if dt in METADATA_KEYS or not isinstance(names, list):
                    continue
                if hasattr(src, dt) and hasattr(dst, dt):
                    available = getattr(src, dt, [])
                    to_load = [e for e in available if e in names]
                    if to_load:
                        setattr(dst, dt, to_load)
        
        # Restore all objects using the new two-phase approach
        restored_objects = set()
        base_objects_to_restore = [obj for obj in full_state['all_objects'] if not obj['is_duplicate']]
        duplicate_objects_to_restore = [obj for obj in full_state['all_objects'] if obj['is_duplicate']]
        
        # STEP 1: Restore all base objects
        for obj_state in base_objects_to_restore:
            base_name = obj_state['base_name']
            
            if base_name not in bpy.data.objects:
                continue
            
            new_obj = bpy.data.objects[base_name]
            restored_objects.add(new_obj.name)
            
            # Restore transforms and properties
            new_obj.location = obj_state['location']
            new_obj.rotation_mode = obj_state['rotation_mode']
            
            if obj_state['rotation_mode'] == 'QUATERNION':
                new_obj.rotation_quaternion = obj_state['rotation']
            elif obj_state['rotation_mode'] == 'AXIS_ANGLE':
                new_obj.rotation_axis_angle = obj_state['rotation']
            else:
                new_obj.rotation_euler = obj_state['rotation']
            
            new_obj.scale = obj_state['scale']
            new_obj.hide_viewport = obj_state['hide_viewport']
            new_obj.hide_render = obj_state['hide_render']
            new_obj.hide_select = obj_state['hide_select']
            
            # Link to collections
            for coll_name in obj_state['collections']:
                coll = bpy.data.collections.get(coll_name)
                if coll and new_obj.name not in coll.objects:
                    coll.objects.link(new_obj)
        
        # STEP 2: Create all duplicates
        for obj_state in duplicate_objects_to_restore:
            obj_name = obj_state['name']
            base_name = obj_state['base_name']
            
            # Special case: if duplicate has same name as base
            if obj_name == base_name and obj_name in bpy.data.objects:
                new_obj = bpy.data.objects[obj_name]
            else:
                base_obj = bpy.data.objects.get(base_name)
                if not base_obj:
                    continue
                
                new_obj = base_obj.copy()
                
                if obj_state['collections']:
                    first_coll = bpy.data.collections.get(obj_state['collections'][0])
                    if first_coll:
                        first_coll.objects.link(new_obj)
                
                new_obj.name = obj_name
            
            restored_objects.add(new_obj.name)
            
            # Restore transforms and properties
            new_obj.location = obj_state['location']
            new_obj.rotation_mode = obj_state['rotation_mode']
            
            if obj_state['rotation_mode'] == 'QUATERNION':
                new_obj.rotation_quaternion = obj_state['rotation']
            elif obj_state['rotation_mode'] == 'AXIS_ANGLE':
                new_obj.rotation_axis_angle = obj_state['rotation']
            else:
                new_obj.rotation_euler = obj_state['rotation']
            
            new_obj.scale = obj_state['scale']
            new_obj.hide_viewport = obj_state['hide_viewport']
            new_obj.hide_render = obj_state['hide_render']
            new_obj.hide_select = obj_state['hide_select']
            
            # Link to remaining collections
            for coll_name in obj_state['collections']:
                coll = bpy.data.collections.get(coll_name)
                if coll and new_obj.name not in coll.objects:
                    coll.objects.link(new_obj)

        link_active_states[fp] = True
        force_viewport_refresh()
        self.report({'INFO'}, f"Reloaded: {os.path.basename(fp)}")
        return {'FINISHED'}

    def _remove_collection_instances(self, context, fp, items):
        """Remove collection instance empties."""
        active_col = context.view_layer.active_layer_collection.collection
        collections = items['collections']
        
        for obj in list(active_col.objects):
            if obj.type == 'EMPTY' and obj.instance_collection:
                coll = obj.instance_collection
                if (normalize_filepath(coll.library.filepath) == fp and 
                    coll.name in collections):
                    bpy.data.objects.remove(obj, do_unlink=True)

    def _relink_collections(self, fp, items, active_col):
        """Re-create collection instances."""
        for coll_name, empty_name in items['collection_instances'].items():
            coll = next((c for c in bpy.data.collections 
                        if c.name == coll_name and safe_library(c) and
                        normalize_filepath(c.library.filepath) == fp), None)
            
            if not coll:
                continue
            
            empty = bpy.data.objects.new(name=empty_name, object_data=None)
            empty.instance_type = 'COLLECTION'
            empty.instance_collection = coll
            empty.rotation_mode = 'QUATERNION'
            active_col.objects.link(empty)
            
            # Restore transforms
            tr = items['transforms'].get(coll_name, {})
            empty.location = tr.get('location', (0, 0, 0))
            empty.rotation_quaternion = tr.get('rotation', (1, 0, 0, 0))
            empty.scale = tr.get('scale', (1, 1, 1))

    def _relink_objects(self, fp, items, active_col):
        """Re-link objects to active collection."""
        for obj_name in items.get('objects', []):
            obj = bpy.data.objects.get(obj_name)
            if obj and safe_library(obj) and normalize_filepath(obj.library.filepath) == fp:
                active_col.objects.link(obj)


class LINKEDITOR_OT_relocate(bpy.types.Operator, ImportHelper):
    """Relocate a linked .blend file to a new filepath."""
    bl_idname = "linkeditor.relocate"
    bl_label = "Relocate Linked File"
    filter_glob: StringProperty(default="*.blend", options={'HIDDEN'})
    original_filepath: StringProperty()

    def execute(self, context):
        new_path = normalize_filepath(self.filepath)
        old_path = normalize_filepath(self.original_filepath)
        
        for lib in bpy.data.libraries:
            if normalize_filepath(lib.filepath) == old_path:
                lib.filepath = new_path
                self.report({'INFO'}, f"Relocated to: {os.path.basename(new_path)}")
                break
        
        return {'FINISHED'}


class LINKEDITOR_OT_remove(bpy.types.Operator):
    """Delete a linked .blend and clean up related data."""
    bl_idname = "linkeditor.remove"
    bl_label = "Delete Linked File"
    filepath: StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        lib = find_library_by_filepath(fp)
        
        if not lib:
            self.report({'ERROR'}, "Linked library not found")
            return {'CANCELLED'}

        # Remove library
        bpy.data.libraries.remove(lib)
        
        # Clean up state
        library_order[:] = [p for p in library_order if normalize_filepath(p) != fp]
        expanded_states.pop(fp, None)
        link_active_states.pop(fp, None)
        linked_elements.pop(fp, None)
        resolution_status.pop(fp, None)
        
        force_viewport_refresh()
        self.report({'INFO'}, f"Removed: {os.path.basename(fp)}")
        return {'FINISHED'}


class LINKEDITOR_OT_switch_mode(bpy.types.Operator, ImportHelper):
    """Switch between low-res and high-res versions of a library."""
    bl_idname = "linkeditor.switch_mode"
    bl_label = "Switch Resolution Mode"
    original_filepath: StringProperty()
    filter_glob: StringProperty(default="*.blend", options={'HIDDEN'})

    def invoke(self, context, event):
        """Check if target file exists, open file browser if not."""
        orig = normalize_filepath(self.original_filepath)
        rs = resolution_status.get(orig, {})
        
        # Determine target path based on resolution status
        if rs:
            tgt = rs["low_path"] if rs["status"] == "high" else rs["high_path"]
        else:
            hi_fp = get_hi_res_path(orig)
            lo_fp = hi_fp[:-6] + LO_SUFFIX
            tgt = lo_fp if orig == hi_fp else hi_fp
        
        # If target file doesn't exist, open file browser
        if not os.path.exists(bpy.path.abspath(tgt)):
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        
        self.filepath = tgt
        return self.execute(context)

    def execute(self, context):
        orig_norm = normalize_filepath(self.original_filepath)
        tgt_fp = normalize_filepath(self.filepath)

        # Check if the library is unloaded
        if orig_norm in link_active_states and not link_active_states[orig_norm]:
            self.report({'WARNING'}, "Turn visibility ON for switching resolution")
            return {'CANCELLED'}

        hi_fp = get_hi_res_path(orig_norm)
        lo_fp = hi_fp[:-6] + LO_SUFFIX
        lib = next((l for l in bpy.data.libraries 
                   if normalize_filepath(l.filepath) in {hi_fp, lo_fp}), None)
        
        if not lib:
            self.report({'ERROR'}, "Linked library not found")
            return {'CANCELLED'}

        # Save current state
        if normalize_filepath(lib.filepath) == hi_fp:
            linked_elements[hi_fp] = get_linked_item_names(lib, context)

        # Clean up ephemeral libraries if switching to hi-res
        if tgt_fp == hi_fp:
            hid = next((h for h in ephemerally_loaded_libraries 
                       if normalize_filepath(h.filepath) == hi_fp), None)
            if hid:
                bpy.data.libraries.remove(hid)
                ephemerally_loaded_libraries.discard(hid)
            ephemeral_hidden_libraries.discard(hi_fp)

        # Capture current state and switch
        current_fp = normalize_filepath(lib.filepath)
        linked_elements[current_fp] = get_linked_item_names(lib, context)
        transforms = linked_elements[current_fp].get('transforms', {})

        lib.filepath = tgt_fp
        reload_library(lib)

        # Re-link objects and collections
        self._relink_items(context, lib, tgt_fp, transforms)

        # Update resolution status
        self._update_resolution_status(orig_norm, tgt_fp, hi_fp, lo_fp)

        # Update state dictionaries
        self._update_state_dicts(orig_norm, tgt_fp)

        force_viewport_refresh()
        self.report({'INFO'}, f"Switched to: {os.path.basename(tgt_fp)}")
        return {'FINISHED'}

    def _relink_items(self, context, lib, fp, transforms):
        """Re-link objects and collections after switching resolution."""
        col = context.view_layer.active_layer_collection.collection
        
        # Link objects
        for obj in bpy.data.objects:
            if obj.library == lib and obj.name not in col.objects:
                col.objects.link(obj)
        
        # Link collections
        for coll in bpy.data.collections:
            if coll.library == lib and coll.name not in col.children:
                col.children.link(coll)

        # Restore transforms for collections
        linked_elements[fp] = get_linked_item_names(lib, context)
        if linked_elements[fp].get('type') == 'collections':
            for coll_name in linked_elements[fp].get('collections', []):
                if coll_name in transforms:
                    for obj in col.objects:
                        if (obj.type == 'EMPTY' and obj.instance_collection and 
                            obj.instance_collection.name == coll_name):
                            obj.rotation_mode = 'QUATERNION'
                            obj.location = transforms[coll_name].get('location', [0, 0, 0])
                            obj.rotation_quaternion = transforms[coll_name].get('rotation', [1, 0, 0, 0])
                            obj.scale = transforms[coll_name].get('scale', [1, 1, 1])
                            break

    def _update_resolution_status(self, orig_norm, tgt_fp, hi_fp, lo_fp):
        """Update resolution status for both high and low paths."""
        # Determine which is high and which is low
        is_orig_lo = (resolution_status.get(orig_norm, {}).get("status") == "low" or 
                     is_lo_file(orig_norm))
        is_target_lo = not is_orig_lo
        high_path = orig_norm if is_target_lo else tgt_fp
        low_path = tgt_fp if is_target_lo else orig_norm

        for key in [high_path, low_path]:
            status = "high" if key == high_path else "low"
            high_res_for_render = resolution_status.get(key, {}).get("high_res_for_render", False)
            resolution_status[key] = {
                "status": status,
                "high_path": high_path,
                "low_path": low_path,
                "high_res_for_render": high_res_for_render,
            }

    def _update_state_dicts(self, orig_norm, tgt_fp):
        """Update library_order, link_active_states, and expanded_states."""
        if orig_norm in library_order:
            idx = library_order.index(orig_norm)
            library_order[idx] = tgt_fp

        if orig_norm in link_active_states:
            link_active_states[tgt_fp] = link_active_states.pop(orig_norm)

        if orig_norm in expanded_states:
            expanded_states[tgt_fp] = expanded_states.pop(orig_norm)

# ============================================================================
# UI PANEL
# ============================================================================

class LINKEDITOR_PT_panel(bpy.types.Panel):
    """Link Manager panel in the 3D viewport sidebar."""
    bl_label = "Link Manager"
    bl_idname = "LINKEDITOR_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Link Manager"

    def draw(self, context):
        layout = self.layout

        layout.label(text="Linked Files:")
        
        # Update library order
        current_norm = [normalize_filepath(lib.filepath) for lib in bpy.data.libraries]
        bases_in_scene = {get_hi_res_path(fp) for fp in current_norm}
        
        # Clean up library_order
        library_order[:] = [fp for fp in library_order 
                           if get_hi_res_path(fp) in bases_in_scene or 
                           fp in link_active_states]
        
        # Add new libraries
        for fp in current_norm:
            if get_hi_res_path(fp) not in {get_hi_res_path(k) for k in library_order}:
                library_order.append(fp)

        # Draw each library
        for fp in library_order:
            live_fp = next((c for c in current_norm 
                           if get_hi_res_path(c) == get_hi_res_path(fp)), fp)
            abs_fp = bpy.path.abspath(live_fp)
            
            # Skip hidden libraries
            if (live_fp in ephemeral_hidden_libraries or 
                resolution_status.get(live_fp, {}).get("hidden")):
                continue

            self._draw_library_row(layout, live_fp, abs_fp)

        layout.separator()
        layout.operator("wm.link", text="Add Link", icon="ADD")

    def _draw_library_row(self, layout, live_fp, abs_fp):
        """Draw a single library row in the UI."""
        expanded = expanded_states.get(live_fp, False)
        is_loaded = link_active_states.get(live_fp, True)
        is_lo = (resolution_status.get(live_fp, {}).get("status") == "low" or 
                (live_fp not in resolution_status and is_lo_file(live_fp)))

        row = layout.row(align=True)
        
        # Expand toggle
        row.operator("linkeditor.toggle_expand", text="",
                    icon="TRIA_DOWN" if expanded else "TRIA_RIGHT",
                    emboss=False).filepath = live_fp
        
        # Library name
        row.label(text=os.path.basename(abs_fp))
        
        # Load/Unload button
        row.operator("linkeditor.load_and_unload", text="",
                    icon="HIDE_OFF" if is_loaded else "HIDE_ON").filepath = live_fp
        
        # Resolution switch button
        row.operator("linkeditor.switch_mode", text="",
                    icon="SPLIT_HORIZONTAL" if is_lo else "VIEW_ORTHO").original_filepath = live_fp
        
        # Render resolution toggle (only for low-res)
        if is_lo:
            hi_r = resolution_status.get(live_fp, {}).get("high_res_for_render", False)
            row.operator("linkeditor.render_resolution", text="",
                        icon="ANTIALIASED" if hi_r else "ALIASED").filepath = live_fp
        else:
            row.label(text="", icon="ANTIALIASED")
        
        # Additional buttons
        row.operator("linkeditor.relocate", text="", 
                    icon="GRAPH").original_filepath = live_fp
        row.operator("linkeditor.reload", text="", 
                    icon="FILE_REFRESH").filepath = live_fp
        row.operator("linkeditor.remove", text="", 
                    icon="X").filepath = live_fp
        
        # Show full path when expanded
        if expanded:
            layout.row().label(text=live_fp)

# ============================================================================
# REGISTRATION
# ============================================================================

classes = (
    LINKEDITOR_OT_toggle_expand,
    LINKEDITOR_OT_load_and_unload,
    LINKEDITOR_OT_relocate,
    LINKEDITOR_OT_reload,
    LINKEDITOR_OT_remove,
    LINKEDITOR_OT_switch_mode,
    LINKEDITOR_OT_render_resolution,
    LINKEDITOR_PT_panel,
)


def _register_handlers():
    """Register event handlers, removing duplicates first."""
    handlers = [
        (bpy.app.handlers.load_post, linkeditor_load_post, 'linkeditor_load_post'),
        (bpy.app.handlers.render_pre, prepare_render, 'prepare_render'),
        (bpy.app.handlers.render_post, restore_render, 'restore_render'),
        (bpy.app.handlers.render_cancel, restore_render, 'restore_render'),
        (bpy.app.handlers.depsgraph_update_post, monitor_libraries, 'monitor_libraries'),
    ]
    
    for handler_list, handler_func, handler_name in handlers:
        # Remove existing handlers
        for handler in handler_list[:]:
            if handler.__name__ == handler_name:
                handler_list.remove(handler)
        # Add new handler
        handler_list.append(handler_func)


def _unregister_handlers():
    """Unregister all event handlers."""
    handlers = [
        (bpy.app.handlers.load_post, 'linkeditor_load_post'),
        (bpy.app.handlers.render_pre, 'prepare_render'),
        (bpy.app.handlers.render_post, 'restore_render'),
        (bpy.app.handlers.render_cancel, 'restore_render'),
        (bpy.app.handlers.depsgraph_update_post, 'monitor_libraries'),
    ]
    
    for handler_list, handler_name in handlers:
        for handler in handler_list[:]:
            if handler.__name__ == handler_name:
                handler_list.remove(handler)


def register():
    """Register all classes and handlers."""
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            pass
    
    _register_handlers()


def unregister():
    """Unregister all classes and handlers."""
    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass
    
    _unregister_handlers()


if __name__ == "__main__":
    try:
        unregister()
    except Exception:
        pass
    register()
