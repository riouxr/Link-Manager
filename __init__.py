bl_info = {
    "name": "Link Manager",
    "author": "Your Name",
    "version": (1, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > Link Manager",
    "description": "List linked files with expand toggle, relocate, reload, delete and add link buttons",
    "category": "Object",
}

import bpy
import os
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ImportHelper

# ────────────────────────────────────────────────────────────────────
#  Globals
# ────────────────────────────────────────────────────────────────────
library_order = []
expanded_states = {}
link_active_states = {}
linked_elements = {} 
resolution_status = {}
ephemerally_loaded_libraries = set()
ephemeral_hidden_libraries = set()
_RENDER_SWAPS = {}
LO_SUFFIX = "_Lo.blend"

# ────────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────────
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
    """Redraw every 3D viewport in every Blender window."""
    bpy.context.view_layer.update()
    wm = bpy.context.window_manager
    if not wm:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()

def reload_library(lib):
    """Version-safe wrapper for Library.reload()."""
    try:
        lib.reload()
    except RuntimeError:
        lib.reload()  # Fallback for Blender 4.2

# ────────────────── Dynamic low/high-res helpers ─────────────────
def is_lo_file(path: str) -> bool:
    """True if path ends with the low-res suffix."""
    return normalize_filepath(path).endswith(LO_SUFFIX)

def get_hi_res_path(path: str) -> str:
    """Convert a low-res path to its hi-res counterpart."""
    p = normalize_filepath(path)
    return p[:-len(LO_SUFFIX)] + ".blend" if is_lo_file(p) else p

def lib_base(path: str) -> str:
    """Strip '.blend' and any low-res suffix to obtain a library base key."""
    p = normalize_filepath(path)
    if p.endswith(LO_SUFFIX):
        return p[:-len(LO_SUFFIX)]
    return p[:-6] if p.lower().endswith(".blend") else p

# ────────────────────────────────────────────────────────────────────
#  Linked-item capture
# ────────────────────────────────────────────────────────────────────
def get_linked_item_names(library):
    try:
        lib_fp_norm = normalize_filepath(library.filepath)
    except ReferenceError:
        return {}

    result = {}
    collections = []
    collection_instances = []
    objects = []
    transforms = {}  # Store transforms for each collection's instance empty

    # Infer linking options
    abs_fp = bpy.path.abspath(library.filepath)
    is_relative = False
    if bpy.context.preferences.filepaths.use_relative_paths:
        try:
            rel_fp = bpy.path.relpath(abs_fp)
            is_relative = library.filepath == rel_fp and library.filepath != abs_fp
        except ValueError:
            pass

    options = {
        "relative_path": is_relative,
        "active_collection": True,
        "instance_collections": False,
        "instance_object_data": False
    }

    # Check for linked collections and their linking method
    active_col = bpy.context.view_layer.active_layer_collection.collection
    for coll in bpy.data.collections:
        lib = safe_library(coll)
        if lib and normalize_filepath(lib.filepath) == lib_fp_norm:
            collections.append(coll.name)
            if coll.name in [c.name for c in active_col.children]:
                options["instance_collections"] = False
            for obj in active_col.objects:
                if obj.type == 'EMPTY' and obj.instance_collection == coll:
                    collection_instances.append(obj.name)
                    options["instance_collections"] = True
                    # Store transforms of the instance empty
                    obj.rotation_mode = 'QUATERNION'  # Ensure quaternion mode
                    transforms[coll.name] = {
                        'location': list(obj.location),
                        'rotation': list(obj.rotation_quaternion),
                        'scale': list(obj.scale)
                    }
                    break

    # Check for objects, including those in collections, and their data
    for obj in bpy.data.objects:
        lib = safe_library(obj)
        if lib and normalize_filepath(lib.filepath) == lib_fp_norm:
            if obj.type == 'EMPTY' and obj.instance_collection:
                if obj.instance_collection.name not in collections:
                    collections.append(obj.instance_collection.name)
                if obj.name not in collection_instances:
                    collection_instances.append(obj.name)
                options["instance_collections"] = True
                obj.rotation_mode = 'QUATERNION'  # Ensure quaternion mode
                transforms[obj.instance_collection.name] = {
                    'location': list(obj.location),
                    'rotation': list(obj.rotation_quaternion),
                    'scale': list(obj.scale)
                }
            else:
                obj_collection_names = [c.name for c in obj.users_collection]
                if obj.name not in collection_instances and (obj.name in [o.name for o in active_col.objects] or any(c in collections for c in obj_collection_names)):
                    if obj.data and safe_library(obj.data) and normalize_filepath(obj.data.library.filepath) == lib_fp_norm:
                        options["instance_object_data"] = True
                        objects.append(obj.name)
                    elif obj.name not in objects:
                        objects.append(obj.name)

    if collections:
        result['type'] = 'collections'
        result['collections'] = collections
        result['collection_instances'] = collection_instances
        result['options'] = options
        result['transforms'] = transforms
        return result
    elif objects:
        result['type'] = 'objects'
        result['objects'] = objects
        result['options'] = options
        return result

    for dt in ('lights', 'materials', 'cameras', 'meshes', 'armatures', 'curves', 'lattices', 'metaballs', 'texts', 'grease_pencils', 'images'):
        names = []
        for item in getattr(bpy.data, dt):
            lib = safe_library(item)
            if lib and normalize_filepath(lib.filepath) == lib_fp_norm:
                names.append(item.name)
                if dt in ('meshes', 'armatures', 'curves', 'lattices', 'metaballs'):
                    options["instance_object_data"] = True
        if names:
            result[dt] = names

    result['type'] = 'other'
    result['options'] = options
    return result

# ────────────────────────────────────────────────────────────────────
#  Hi-res loader (hidden)
# ────────────────────────────────────────────────────────────────────
def load_highres_hidden(lo_fp):
    def base(name):
        for suf in ("_Lo", "_lo", "_Low", "_low"):
            if name.endswith(suf):
                return name[:-len(suf)]
        return name

    hi_fp = resolution_status[lo_fp].get("high_path")
    if not hi_fp or not os.path.exists(bpy.path.abspath(hi_fp)):
        return False

    need_meshes = set()
    need_colls = set()
    for obj in bpy.data.objects:
        lib = safe_library(obj)
        if not lib or normalize_filepath(lib.filepath) != lo_fp:
            continue
        if obj.type == 'MESH':
            need_meshes.add(base(obj.data.name))
        elif obj.type == 'EMPTY' and obj.instance_collection:
            need_colls.add(base(obj.instance_collection.name))

    if not need_meshes and not need_colls:
        return False

    try:
        with bpy.data.libraries.load(hi_fp, link=True) as (src, dst):
            dst.meshes = [m for m in src.meshes if base(m) in need_meshes]
            dst.collections = [c for c in src.collections if base(c) in need_colls]
    except Exception:
        return False

    lib = next((l for l in bpy.data.libraries
                if normalize_filepath(l.filepath) == hi_fp), None)
    if lib:
        ephemerally_loaded_libraries.add(lib)
        ephemeral_hidden_libraries.add(hi_fp)
        return True
    return False

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
    for lib in bpy.data.libraries:
        fp = normalize_filepath(lib.filepath)
        if fp not in linked_elements:
            linked_elements[fp] = get_linked_item_names(lib)

# ────────────────────────────────────────────────────────────────────
#  Render-time swapping
# ────────────────────────────────────────────────────────────────────
@persistent
def prepare_render(scene, _):
    for lo_fp, rs in resolution_status.items():
        if not rs.get("high_res_for_render"):
            continue
        hi_fp = rs["high_path"]
        base = lib_base(lo_fp)
        lib = next((l for l in bpy.data.libraries
                    if lib_base(l.filepath) == base), None)
        if not lib or normalize_filepath(lib.filepath) == hi_fp:
            continue
        _RENDER_SWAPS[base] = lib.filepath
        lib.filepath = hi_fp
        reload_library(lib)
    bpy.context.view_layer.update()

@persistent
def restore_render(scene, _):
    for lib in bpy.data.libraries:
        base = lib_base(lib.filepath)
        orig_low = _RENDER_SWAPS.pop(base, None)
        if not orig_low or normalize_filepath(lib.filepath) == orig_low:
            continue
        lib.filepath = orig_low
        reload_library(lib)
    bpy.context.view_layer.update()
    force_viewport_refresh()

# ────────────────────────────────────────────────────────────────────
#  Operators
# ────────────────────────────────────────────────────────────────────
class LINKEDITOR_OT_render_resolution(bpy.types.Operator):
    """Toggle whether this low-res library is swapped to Hi-res at render time."""
    bl_idname = "linkeditor.render_resolution"
    bl_label = "Toggle Render Resolution"
    filepath: bpy.props.StringProperty()

    def execute(self, context):
        lo_fp = normalize_filepath(self.filepath)
        if not is_lo_file(lo_fp):
            self.report({'WARNING'}, f"Works only on *{LO_SUFFIX} files.")
            return {'CANCELLED'}
        rs = resolution_status.setdefault(
            lo_fp, {
                "status": "low",
                "low_path": lo_fp,
                "high_path": get_hi_res_path(lo_fp),
                "high_res_for_render": False,
            })
        rs["high_res_for_render"] ^= True
        force_viewport_refresh()
        state = "ON" if rs["high_res_for_render"] else "OFF"
        self.report({'INFO'}, f"Hi-res render {state}.")
        return {'FINISHED'}

class LINKEDITOR_OT_load_and_unload(bpy.types.Operator):
    """Unload a library if it’s loaded, or re-link it if it was unloaded."""
    bl_idname = "linkeditor.load_and_unload"
    bl_label = "Load/Unload Linked File"
    filepath: bpy.props.StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) == fp), None)
        if lib:
            linked_elements[fp] = get_linked_item_names(lib)
            if linked_elements[fp].get('type') == 'collections':
                active_col = context.view_layer.active_layer_collection.collection
                collections = linked_elements[fp].get('collections', [])
                for obj in active_col.objects[:]:
                    if obj.type == 'EMPTY':
                        coll = obj.instance_collection
                        if (coll and coll.name in collections and safe_library(coll) and normalize_filepath(coll.library.filepath) == fp) or \
                           (not safe_library(obj) and (obj.name in collections or '_instance' in obj.name)):
                            bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.libraries.remove(lib)
            link_active_states[fp] = False
            force_viewport_refresh()
            self.report({'INFO'}, f"Unloaded: {os.path.basename(fp)}")
            return {'FINISHED'}

        if fp in linked_elements:
            options = linked_elements[fp].get('options', {
                'relative_path': False,
                'active_collection': True,
                'instance_collections': bool(linked_elements[fp].get('collection_instances', [])),
                'instance_object_data': False
            }).copy()
            transforms = linked_elements[fp].get('transforms', {})

            try:
                with bpy.data.libraries.load(fp, link=True) as (src, dst):
                    for dt, names in linked_elements[fp].items():
                        if dt not in ('options', 'collection_instances', 'type', 'transforms'):
                            setattr(dst, dt, [e for e in getattr(src, dt) if e in names])
            except Exception as e:
                self.report({'ERROR'}, f"Failed to reload library: {e}")
                return {'CANCELLED'}

            datablock_type = linked_elements[fp].get('type', 'objects')
            active_col = context.view_layer.active_layer_collection.collection

            def get_all_collections_with(coll_name):
                result = []
                def check_collection(col, path):
                    if coll_name in [c.name for c in col.children]:
                        result.append(f"{path}/{col.name}")
                    for child in col.children:
                        check_collection(child, f"{path}/{col.name}")
                check_collection(bpy.context.scene.collection, "Scene Collection")
                return result

            for obj in active_col.objects[:]:
                if obj.type == 'EMPTY':
                    lib = safe_library(obj)
                    if (lib and normalize_filepath(lib.filepath) == fp) or \
                       (obj.instance_collection and not bpy.data.collections.get(obj.instance_collection.name)) or \
                       (not lib and '_instance' in obj.name):
                        bpy.data.objects.remove(obj, do_unlink=True)

            if datablock_type == 'collections':
                collections = linked_elements[fp].get('collections', [])
                linked_elements[fp]['collection_instances'] = []

                def unlink_collection(coll_name):
                    def process_collection(col):
                        for child in col.children[:]:
                            if child.name == coll_name and safe_library(child) and normalize_filepath(child.library.filepath) == fp:
                                col.children.unlink(child)
                            process_collection(child)
                    process_collection(bpy.context.scene.collection)
                for coll_name in collections:
                    unlink_collection(coll_name)

                for coll_name in collections:
                    coll = bpy.data.collections.get(coll_name)
                    if not coll:
                        continue
                    if options['instance_collections']:
                        instance_names = linked_elements[fp].get('collection_instances', [])
                        empty_name = instance_names[0] if instance_names else f"{coll_name}_instance"
                        empty = bpy.data.objects.new(name=empty_name, object_data=None)
                        empty.instance_type = 'COLLECTION'
                        empty.instance_collection = coll
                        empty.rotation_mode = 'QUATERNION'  # Ensure quaternion mode
                        if empty.name not in [o.name for o in active_col.objects]:
                            active_col.objects.link(empty)
                        # Apply stored transforms if available
                        if coll_name in transforms:
                            empty.location = transforms[coll_name].get('location', [0, 0, 0])
                            empty.rotation_quaternion = transforms[coll_name].get('rotation', [1, 0, 0, 0])
                            empty.scale = transforms[coll_name].get('scale', [1, 1, 1])
                        linked_elements[fp]['collection_instances'].append(empty.name)
                    else:
                        if coll.name not in [c.name for c in active_col.children]:
                            active_col.children.link(coll)

            else:
                valid_objects = linked_elements[fp].get('objects', [])
                for obj in dst.objects:
                    if obj and obj.name in valid_objects and obj.type != 'EMPTY':
                        obj.parent = None
                        active_col.objects.link(obj)

            for obj in active_col.objects[:]:
                if obj.type == 'EMPTY' and obj.name not in linked_elements[fp].get('collection_instances', []):
                    bpy.data.objects.remove(obj, do_unlink=True)

            lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) == fp), None)
            if lib and options['relative_path']:
                try:
                    lib.filepath = bpy.path.relpath(bpy.path.abspath(fp))
                except ValueError:
                    pass

            link_active_states[fp] = True
            force_viewport_refresh()
            self.report({'INFO'}, f"Reloaded: {os.path.basename(fp)}")
            return {'FINISHED'}

        self.report({'WARNING'}, "No library to unload or reload")
        return {'CANCELLED'}

class LINKEDITOR_OT_reload(bpy.types.Operator):
    """Reload a linked .blend, preserving only the previously visible items."""
    bl_idname = "linkeditor.reload"
    bl_label = "Reload Linked File"
    filepath: bpy.props.StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) == fp), None)
        if lib:
            linked_elements[fp] = get_linked_item_names(lib)
            if linked_elements[fp].get('type') == 'collections':
                active_col = context.view_layer.active_layer_collection.collection
                collections = linked_elements[fp].get('collections', [])
                for obj in active_col.objects[:]:
                    if obj.type == 'EMPTY':
                        coll = obj.instance_collection
                        if (coll and coll.name in collections and safe_library(coll) and normalize_filepath(coll.library.filepath) == fp) or \
                           (not safe_library(obj) and (obj.name in collections or '_instance' in obj.name)):
                            bpy.data.objects.remove(obj, do_unlink=True)
            try:
                bpy.data.libraries.remove(lib)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to unload before reload: {e}")
                return {'CANCELLED'}

        items = linked_elements.get(fp)
        if not items:
            self.report({'WARNING'}, "No items found to reload")
            return {'CANCELLED'}

        options = items.get('options', {
            'relative_path': False,
            'active_collection': True,
            'instance_collections': bool(items.get('collection_instances', [])),
            'instance_object_data': False
        }).copy()
        transforms = items.get('transforms', {})

        try:
            with bpy.data.libraries.load(fp, link=True) as (src, dst):
                for dt, names in items.items():
                    if dt not in ('options', 'collection_instances', 'type', 'transforms'):
                        setattr(dst, dt, [e for e in getattr(src, dt) if e in names])
        except Exception as e:
            self.report({'ERROR'}, f"Failed to reload library: {e}")
            return {'CANCELLED'}

        datablock_type = items.get('type', 'objects')
        active_col = context.view_layer.active_layer_collection.collection

        def get_all_collections_with(coll_name):
            result = []
            def check_collection(col, path):
                if coll_name in [c.name for c in col.children]:
                    result.append(f"{path}/{col.name}")
                for child in col.children:
                    check_collection(child, f"{path}/{col.name}")
            check_collection(bpy.context.scene.collection, "Scene Collection")
            return result

        for obj in active_col.objects[:]:
            if obj.type == 'EMPTY':
                lib = safe_library(obj)
                if (lib and normalize_filepath(lib.filepath) == fp) or \
                   (obj.instance_collection and not bpy.data.collections.get(obj.instance_collection.name)) or \
                   (not lib and '_instance' in obj.name):
                    bpy.data.objects.remove(obj, do_unlink=True)

        if datablock_type == 'collections':
            collections = items.get('collections', [])
            items['collection_instances'] = []

            def unlink_collection(coll_name):
                def process_collection(col):
                    for child in col.children[:]:
                        if child.name == coll_name and safe_library(child) and normalize_filepath(child.library.filepath) == fp:
                            col.children.unlink(child)
                        process_collection(child)
                process_collection(bpy.context.scene.collection)
            for coll_name in collections:
                unlink_collection(coll_name)

            for coll_name in collections:
                coll = bpy.data.collections.get(coll_name)
                if not coll:
                    continue
                if options['instance_collections']:
                    instance_names = items.get('collection_instances', [])
                    empty_name = instance_names[0] if instance_names else f"{coll_name}_instance"
                    empty = bpy.data.objects.new(name=empty_name, object_data=None)
                    empty.instance_type = 'COLLECTION'
                    empty.instance_collection = coll
                    empty.rotation_mode = 'QUATERNION'  # Ensure quaternion mode
                    if empty.name not in [o.name for o in active_col.objects]:
                        active_col.objects.link(empty)
                    if coll_name in transforms:
                        empty.location = transforms[coll_name].get('location', [0, 0, 0])
                        empty.rotation_quaternion = transforms[coll_name].get('rotation', [1, 0, 0, 0])
                        empty.scale = transforms[coll_name].get('scale', [1, 1, 1])
                    items['collection_instances'].append(empty.name)
                else:
                    if coll.name not in [c.name for c in active_col.children]:
                        active_col.children.link(coll)

        else:
            valid_objects = items.get('objects', [])
            for obj in dst.objects:
                if obj and obj.name in valid_objects and obj.type != 'EMPTY':
                    obj.parent = None
                    active_col.objects.link(obj)

        for obj in active_col.objects[:]:
            if obj.type == 'EMPTY' and obj.name not in items.get('collection_instances', []):
                bpy.data.objects.remove(obj, do_unlink=True)

        lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) == fp), None)
        if lib and options['relative_path']:
            try:
                lib.filepath = bpy.path.relpath(bpy.path.abspath(fp))
            except ValueError:
                pass

        link_active_states[fp] = True
        force_viewport_refresh()
        self.report({'INFO'}, f"Reloaded: {os.path.basename(fp)}")
        return {'FINISHED'}
    
class LINKEDITOR_OT_relocate(bpy.types.Operator, ImportHelper):
    bl_idname = "linkeditor.relocate"
    bl_label = "Relocate Linked File"
    filter_glob: bpy.props.StringProperty(default="*.blend", options={'HIDDEN'})
    original_filepath: bpy.props.StringProperty()

    def execute(self, _):
        new = normalize_filepath(self.filepath)
        old = normalize_filepath(self.original_filepath)
        for lib in bpy.data.libraries:
            if normalize_filepath(lib.filepath) == old:
                lib.filepath = new
                break
        return {'FINISHED'}


class LINKEDITOR_OT_remove(bpy.types.Operator):
    """Delete a linked .blend (both hi- and lo-res variants if present)."""
    bl_idname = "linkeditor.remove"
    bl_label = "Delete Linked File"
    filepath: bpy.props.StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        if is_lo_file(fp):
            lo_fp = fp
            hi_fp = fp[:-len(LO_SUFFIX)] + ".blend"
        else:
            hi_fp = fp
            lo_fp = fp[:-6] + LO_SUFFIX

        targets = {lo_fp, hi_fp}
        lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) in targets), None)
        if not lib:
            self.report({'WARNING'}, "Library not found")
            return {'CANCELLED'}

        name = os.path.basename(normalize_filepath(lib.filepath))

        # Remove instance empties for collections
        if fp in linked_elements and 'collections' in linked_elements[fp]:
            collections = linked_elements[fp]['collections']
            for obj in bpy.data.objects:
                if obj.type == 'EMPTY' and obj.instance_collection and obj.instance_collection.name in collections:
                    bpy.data.objects.remove(obj, do_unlink=True)

        try:
            bpy.data.libraries.remove(lib)
        except RuntimeError as e:
            self.report({'ERROR'}, f"Could not delete library: {e}")
            return {'CANCELLED'}

        for key in targets:
            link_active_states.pop(key, None)
            linked_elements.pop(key, None)
            resolution_status.pop(key, None)

        self.report({'INFO'}, f"Deleted: {name}")
        force_viewport_refresh()
        return {'FINISHED'}

class LINKEDITOR_OT_toggle_expand(bpy.types.Operator):
    bl_idname = "linkeditor.toggle_expand"
    bl_label = "Toggle Expand"
    filepath: bpy.props.StringProperty()

    def execute(self, _):
        n = normalize_filepath(self.filepath)
        expanded_states[n] = not expanded_states.get(n, False)
        return {'FINISHED'}

class LINKEDITOR_OT_switch_mode(bpy.types.Operator, ImportHelper):
    """Switch between Hi-res and Low-res versions of the linked library."""
    bl_idname = "linkeditor.switch_mode"
    bl_label = "Switch Mode"
    original_filepath: bpy.props.StringProperty()
    filter_glob: bpy.props.StringProperty(default="*.blend", options={'HIDDEN'})

    def invoke(self, context, _):
        orig = normalize_filepath(self.original_filepath)
        hi_fp = get_hi_res_path(orig)
        lo_fp = hi_fp[:-6] + LO_SUFFIX
        tgt = lo_fp if orig == hi_fp else hi_fp
        if not os.path.exists(bpy.path.abspath(tgt)):
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}
        self.filepath = tgt
        return self.execute(context)

    def execute(self, context):
        hi_fp = get_hi_res_path(normalize_filepath(self.original_filepath))
        lo_fp = hi_fp[:-6] + LO_SUFFIX
        tgt = normalize_filepath(self.filepath)
        lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) in {hi_fp, lo_fp}), None)
        if not lib:
            self.report({'ERROR'}, "Linked library not found")
            return {'CANCELLED'}

        if normalize_filepath(lib.filepath) == hi_fp:
            linked_elements[hi_fp] = get_linked_item_names(lib)

        if tgt == hi_fp:
            hid = next((h for h in ephemerally_loaded_libraries if normalize_filepath(h.filepath) == hi_fp), None)
            if hid:
                bpy.data.libraries.remove(hid)
                ephemerally_loaded_libraries.discard(hid)
            ephemeral_hidden_libraries.discard(hi_fp)

        # Store transforms from current library before switching
        current_fp = normalize_filepath(lib.filepath)
        linked_elements[current_fp] = get_linked_item_names(lib)
        transforms = linked_elements[current_fp].get('transforms', {})

        lib.filepath = tgt
        reload_library(lib)

        col = context.view_layer.active_layer_collection.collection
        for obj in bpy.data.objects:
            if obj.library == lib and obj.name not in col.objects:
                col.objects.link(obj)
        for coll in bpy.data.collections:
            if coll.library == lib and coll.name not in col.children:
                col.children.link(coll)

        # Apply stored transforms to matching collections in the new library
        tgt_fp = normalize_filepath(tgt)
        linked_elements[tgt_fp] = get_linked_item_names(lib)
        if linked_elements[tgt_fp].get('type') == 'collections':
            for coll_name in linked_elements[tgt_fp].get('collections', []):
                if coll_name in transforms:
                    for obj in col.objects:
                        if obj.type == 'EMPTY' and obj.instance_collection and obj.instance_collection.name == coll_name:
                            obj.rotation_mode = 'QUATERNION'  # Ensure quaternion mode
                            obj.location = transforms[coll_name].get('location', [0, 0, 0])
                            obj.rotation_quaternion = transforms[coll_name].get('rotation', [1, 0, 0, 0])
                            obj.scale = transforms[coll_name].get('scale', [1, 1, 1])
                            break

        rs = resolution_status.setdefault(hi_fp, {"high_path": hi_fp, "low_path": lo_fp})
        rs["status"] = "high" if tgt == hi_fp else "low"

        for hlib in list(ephemerally_loaded_libraries):
            if normalize_filepath(hlib.filepath) == hi_fp and hlib is not lib:
                try:
                    bpy.data.libraries.remove(hlib)
                except RuntimeError:
                    pass
                ephemerally_loaded_libraries.discard(hlib)
                ephemeral_hidden_libraries.discard(hi_fp)

        force_viewport_refresh()
        return {'FINISHED'}
# ────────────────────────────────────────────────────────────────────
#  UI Panel
# ────────────────────────────────────────────────────────────────────
class LINKEDITOR_PT_panel(bpy.types.Panel):
    bl_label = "Link Manager"
    bl_idname = "LINKEDITOR_PT_panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Link Manager"

    def draw(self, context):
        layout = self.layout
        base = get_hi_res_path

        layout.label(text="Linked Files:")
        current_norm = [normalize_filepath(l.filepath) for l in bpy.data.libraries]
        bases_in_scene = {base(fp) for fp in current_norm}
        for fp in current_norm:
            if all(base(fp) != base(k) for k in library_order):
                library_order.append(fp)

        show = [fp for fp in library_order if base(fp) in bases_in_scene or fp in link_active_states]
        for fp in show:
            live_fp = next((c for c in current_norm if base(c) == base(fp)), fp)
            abs_fp = bpy.path.abspath(live_fp)
            if live_fp in ephemeral_hidden_libraries or resolution_status.get(live_fp, {}).get("hidden"):
                continue

            expanded = expanded_states.get(base(fp), False)
            row = layout.row(align=True)
            row.operator("linkeditor.toggle_expand", text="",
                         icon="TRIA_DOWN" if expanded else "TRIA_RIGHT",
                         emboss=False).filepath = base(fp)
            row.label(text=os.path.basename(abs_fp))
            is_loaded = link_active_states.get(base(fp), True)
            row.operator("linkeditor.load_and_unload", text="",
                         icon="HIDE_OFF" if is_loaded else "HIDE_ON").filepath = live_fp
            is_lo = is_lo_file(abs_fp)
            row.operator("linkeditor.switch_mode", text="",
                         icon="SPLIT_HORIZONTAL" if is_lo else "VIEW_ORTHO").original_filepath = live_fp
            if is_lo:
                hi_r = resolution_status.get(live_fp, {}).get("high_res_for_render", False)
                row.operator("linkeditor.render_resolution", text="",
                             icon="ANTIALIASED" if hi_r else "ALIASED").filepath = live_fp
            else:
                row.label(text="", icon="ANTIALIASED")
            row.operator("linkeditor.relocate", text="", icon="GRAPH").original_filepath = live_fp
            row.operator("linkeditor.reload", text="", icon="FILE_REFRESH").filepath = live_fp
            row.operator("linkeditor.remove", text="", icon="X").filepath = live_fp
            if expanded:
                layout.row().label(text=live_fp)

        layout.separator()
        layout.operator("wm.link", text="Add Link", icon="ADD")

# ────────────────────────────────────────────────────────────────────
#  Registration
# ────────────────────────────────────────────────────────────────────
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

def register():
    for c in classes:
        try:
            bpy.utils.register_class(c)
        except ValueError:
            pass
    # Clear all existing handlers to prevent conflicts
    for handler in bpy.app.handlers.load_post[:]:
        if handler.__name__ == 'linkeditor_load_post':
            bpy.app.handlers.load_post.remove(handler)
    for handler in bpy.app.handlers.render_pre[:]:
        if handler.__name__ == 'prepare_render':
            bpy.app.handlers.render_pre.remove(handler)
    for handler in bpy.app.handlers.render_post[:]:
        if handler.__name__ == 'restore_render':
            bpy.app.handlers.render_post.remove(handler)
    for handler in bpy.app.handlers.render_cancel[:]:
        if handler.__name__ == 'restore_render':
            bpy.app.handlers.render_cancel.remove(handler)
    for handler in bpy.app.handlers.depsgraph_update_post[:]:
        if handler.__name__ == 'monitor_libraries':
            bpy.app.handlers.depsgraph_update_post.remove(handler)
    # Register new handlers
    bpy.app.handlers.load_post.append(linkeditor_load_post)
    bpy.app.handlers.render_pre.append(prepare_render)
    bpy.app.handlers.render_post.append(restore_render)
    bpy.app.handlers.render_cancel.append(restore_render)
    bpy.app.handlers.depsgraph_update_post.append(monitor_libraries)

def unregister():
    for c in reversed(classes):
        try:
            bpy.utils.unregister_class(c)
        except RuntimeError:
            pass
    for handler in bpy.app.handlers.load_post[:]:
        if handler.__name__ == 'linkeditor_load_post':
            bpy.app.handlers.load_post.remove(handler)
    for handler in bpy.app.handlers.render_pre[:]:
        if handler.__name__ == 'prepare_render':
            bpy.app.handlers.render_pre.remove(handler)
    for handler in bpy.app.handlers.render_post[:]:
        if handler.__name__ == 'restore_render':
            bpy.app.handlers.render_post.remove(handler)
    for handler in bpy.app.handlers.render_cancel[:]:
        if handler.__name__ == 'restore_render':
            bpy.app.handlers.render_cancel.remove(handler)
    for handler in bpy.app.handlers.depsgraph_update_post[:]:
        if handler.__name__ == 'monitor_libraries':
            bpy.app.handlers.depsgraph_update_post.remove(handler)
            
if __name__ == "__main__":
    try:
        unregister()
    except Exception as e:
        print(f"Error during unregister: {e}")
    register()