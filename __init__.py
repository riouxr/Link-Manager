bl_info = {
    "name": "Link Manager",
    "author": "Your Name",
    "version": (1, 0, 0),
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

# ### Globals
library_order = []
expanded_states = {}
link_active_states = {}
linked_elements = {}
resolution_status = {}
ephemerally_loaded_libraries = set()
ephemeral_hidden_libraries = set()
_RENDER_SWAPS = {}
LO_SUFFIX = "_Lo.blend"

    
# ### Helpers
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
    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D':
            area.tag_redraw()
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'WINDOW':
                        region.tag_redraw()

def reload_library(lib):
    """Version-safe wrapper for Library.reload()."""
    try:
        lib.reload()
    except RuntimeError:
        lib.reload()  # Fallback for Blender 4.2

# #### Dynamic Low/High-Res Helpers
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

# ### Linked-Item Capture
def get_linked_item_names(library):
    try:
        lib_fp_norm = normalize_filepath(library.filepath)
    except ReferenceError:
        return {}

    result = {}
    collections = []
    collection_instances = {}  # Dictionary to map collection names to empty names
    objects = []
    transforms = {}

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

    active_col = bpy.context.view_layer.active_layer_collection.collection
    for coll in bpy.data.collections:
        lib = safe_library(coll)
        if lib and normalize_filepath(lib.filepath) == lib_fp_norm:
            collections.append(coll.name)
            is_instanced = False
            for obj in bpy.data.objects:
                if obj.type == 'EMPTY' and obj.instance_collection == coll:
                    empty_name = obj.name if obj.name and obj.name != "Collection_Instances" else coll.name
                    collection_instances[coll.name] = empty_name
                    is_instanced = True
                    obj.rotation_mode = 'QUATERNION'
                    transforms[coll.name] = {
                        'location': list(obj.location),
                        'rotation': list(obj.rotation_quaternion),
                        'scale': list(obj.scale)
                    }
                    break
            options["instance_collections"] = is_instanced
            if not is_instanced and coll.name in [c.name for c in active_col.children]:
                options["instance_collections"] = False

    for obj in bpy.data.objects:
        lib = safe_library(obj)
        if lib and normalize_filepath(lib.filepath) == lib_fp_norm:
            if obj.type == 'EMPTY' and obj.instance_collection:
                if obj.instance_collection.name not in collections:
                    collections.append(obj.instance_collection.name)
                if obj.instance_collection.name not in collection_instances:
                    collection_instances[obj.instance_collection.name] = obj.name
                options["instance_collections"] = True
                obj.rotation_mode = 'QUATERNION'
                transforms[obj.instance_collection.name] = {
                    'location': list(obj.location),
                    'rotation': list(obj.rotation_quaternion),
                    'scale': list(obj.scale)
                }
            else:
                obj_collection_names = [c.name for c in obj.users_collection]
                if obj.name not in collection_instances.values() and (obj.name in [o.name for o in active_col.objects] or any(c in collections for c in obj_collection_names)):
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

# ### Hi-Res Loader (Hidden)
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

    lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) == hi_fp), None)
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

# ### Render-Time Swapping
@persistent
def prepare_render(scene, _):
    for lo_fp, rs in resolution_status.items():
        if not rs.get("high_res_for_render"):
            continue
        hi_fp = rs["high_path"]
        base = lib_base(lo_fp)
        lib = next((l for l in bpy.data.libraries if lib_base(l.filepath) == base), None)
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

# ### Operators
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
    filepath: StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) == fp), None)

        # --- Unload existing library ---
        if lib:
            linked_elements[fp] = get_linked_item_names(lib)
            if linked_elements[fp].get('type') == 'collections':
                active_col = context.view_layer.active_layer_collection.collection
                collections = linked_elements[fp]['collections']
                for obj in list(active_col.objects):
                    if obj.type == 'EMPTY' and obj.instance_collection:
                        coll = obj.instance_collection
                        coll_lib = safe_library(coll)
                        if coll_lib and normalize_filepath(coll_lib.filepath) == fp and coll.name in collections:
                            bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.libraries.remove(lib)
            link_active_states[fp] = False
            force_viewport_refresh()
            self.report({'INFO'}, f"Unloaded: {os.path.basename(fp)}")
            return {'FINISHED'}

        # --- Reload library if previously known ---
        elif fp in linked_elements:
            options = linked_elements[fp].get('options', {}).copy()
            transforms = linked_elements[fp].get('transforms', {})
            previous_instances = linked_elements[fp].get('collection_instances', {})

            with bpy.data.libraries.load(fp, link=True) as (src, dst):
                for dt, names in linked_elements[fp].items():
                    if dt not in ('options', 'collection_instances', 'type', 'transforms'):
                        setattr(dst, dt, [e for e in getattr(src, dt) if e in names])

            active_col = context.view_layer.active_layer_collection.collection
            # remove old empties
            for obj in list(active_col.objects):
                if obj.type == 'EMPTY' and obj.instance_collection:
                    coll = obj.instance_collection
                    if safe_library(coll) and normalize_filepath(coll.library.filepath) == fp:
                        bpy.data.objects.remove(obj, do_unlink=True)

            if linked_elements[fp]['type'] == 'collections':
                for coll_name in linked_elements[fp]['collections']:
                    coll = next((c for c in bpy.data.collections if c.name == coll_name
                                 and safe_library(c)
                                 and normalize_filepath(c.library.filepath) == fp), None)
                    if not coll:
                        continue
                    if options.get('instance_collections'):
                        empty_name = previous_instances.get(coll_name) or f"{coll_name}_instance"
                        count = 1
                        while empty_name in bpy.data.objects:
                            empty_name = f"{coll_name}_instance.{count:03d}"
                            count += 1
                        empty = bpy.data.objects.new(name=empty_name, object_data=None)
                        empty.instance_type = 'COLLECTION'
                        empty.instance_collection = coll
                        empty.rotation_mode = 'QUATERNION'
                        active_col.objects.link(empty)
                        tr = transforms.get(coll_name, {})
                        empty.location = tr.get('location', (0,0,0))
                        empty.rotation_quaternion = tr.get('rotation', (1,0,0,0))
                        empty.scale = tr.get('scale', (1,1,1))
            else:
                for obj_name in linked_elements[fp].get('objects', []):
                    obj = bpy.data.objects.get(obj_name)
                    if obj and safe_library(obj) and normalize_filepath(obj.library.filepath) == fp:
                        active_col.objects.link(obj)

            lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) == fp), None)
            if lib and options.get('relative_path'):
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


# -------------------------------------------------
# Operator: Reload
# -------------------------------------------------
class LINKEDITOR_OT_reload(bpy.types.Operator):
    """Reload a linked .blend, preserving only the previously visible items."""
    bl_idname = "linkeditor.reload"
    bl_label = "Reload Linked File"
    filepath: StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) == fp), None)

        # unload if loaded
        if lib:
            linked_elements[fp] = get_linked_item_names(lib)
            if linked_elements[fp]['type'] == 'collections':
                active_col = context.view_layer.active_layer_collection.collection
                collections = linked_elements[fp]['collections']
                for obj in list(active_col.objects):
                    if obj.type == 'EMPTY' and obj.instance_collection:
                        coll = obj.instance_collection
                        if normalize_filepath(coll.library.filepath) == fp and coll.name in collections:
                            bpy.data.objects.remove(obj, do_unlink=True)
            bpy.data.libraries.remove(lib)

        items = linked_elements.get(fp)
        if not items:
            self.report({'WARNING'}, "No items found to reload")
            return {'CANCELLED'}

        with bpy.data.libraries.load(fp, link=True) as (src, dst):
            for dt, names in items.items():
                if dt in ('options', 'collection_instances', 'type', 'transforms'):
                    continue
                setattr(dst, dt, [n for n in names if n in getattr(src, dt, [])])

        active_col = context.view_layer.active_layer_collection.collection
        if items['type'] == 'collections':
            for coll_name, empty_name in items['collection_instances'].items():
                coll = next((c for c in bpy.data.collections if c.name == coll_name
                             and safe_library(c)
                             and normalize_filepath(c.library.filepath) == fp), None)
                if not coll:
                    continue
                empty = bpy.data.objects.new(name=empty_name, object_data=None)
                empty.instance_type = 'COLLECTION'
                empty.instance_collection = coll
                empty.rotation_mode = 'QUATERNION'
                active_col.objects.link(empty)
                tr = items['transforms'].get(coll_name, {})
                empty.location = tr.get('location', (0,0,0))
                empty.rotation_quaternion = tr.get('rotation', (1,0,0,0))
                empty.scale = tr.get('scale', (1,1,1))
        else:
            for obj_name in items.get('objects', []):
                obj = bpy.data.objects.get(obj_name)
                if obj and safe_library(obj) and normalize_filepath(obj.library.filepath) == fp:
                    active_col.objects.link(obj)

        if lib and items.get('options', {}).get('relative_path'):
            try:
                lib.filepath = bpy.path.relpath(bpy.path.abspath(fp))
            except ValueError:
                pass

        link_active_states[fp] = True
        force_viewport_refresh()
        self.report({'INFO'}, f"Reloaded: {os.path.basename(fp)}")
        return {'FINISHED'}
# -------------------------------------------------
# Operator: Remove
# -------------------------------------------------

    
class LINKEDITOR_OT_relocate(bpy.types.Operator, ImportHelper):
    """Relocate a linked .blend file to a new filepath."""
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
    """Delete a linked .blend (handles hi- and lo-res) and re-link collections from other active libraries."""
    bl_idname = "linkeditor.remove"
    bl_label = "Delete Linked File"
    filepath: StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)
        # determine low/high paths
        if is_lo_file(fp):
            lo_fp, hi_fp = fp, fp[:-len(LO_SUFFIX)] + ".blend"
        else:
            hi_fp, lo_fp = fp, fp[:-6] + LO_SUFFIX
        targets = {lo_fp, hi_fp}
        lib = next((l for l in bpy.data.libraries if normalize_filepath(l.filepath) in targets), None)
        if not lib:
            self.report({'WARNING'}, "Library not found")
            return {'CANCELLED'}
        name = os.path.basename(normalize_filepath(lib.filepath))

        active_col = context.view_layer.active_layer_collection.collection
        # remove empties only from this file
        if fp in linked_elements and 'collections' in linked_elements[fp]:
            for obj in list(active_col.objects):
                if obj.type == 'EMPTY' and obj.instance_collection:
                    coll = obj.instance_collection
                    coll_lib = safe_library(coll)
                    if coll_lib and normalize_filepath(coll_lib.filepath) == fp and coll.name in linked_elements[fp]['collections']:
                        bpy.data.objects.remove(obj, do_unlink=True)
        # remove the library data
        try:
            bpy.data.libraries.remove(lib)
        except RuntimeError as e:
            self.report({'ERROR'}, f"Could not delete library: {e}")
            return {'CANCELLED'}

        # cleanup internal state
        link_active_states.pop(lo_fp, None)
        link_active_states.pop(hi_fp, None)
        linked_elements.pop(lo_fp, None)
        linked_elements.pop(hi_fp, None)
        resolution_status.pop(lo_fp, None)
        resolution_status.pop(hi_fp, None)

        # re-link collections for other active libraries to restore their empties
        for other_fp, active in list(link_active_states.items()):
            if not active:
                continue
            info = linked_elements.get(other_fp)
            if not info or info.get('type') != 'collections':
                continue
            for coll_name in info['collections']:
                # find the right collection by filepath
                coll = next((c for c in bpy.data.collections
                             if c.name == coll_name
                             and safe_library(c)
                             and normalize_filepath(c.library.filepath) == other_fp), None)
                if not coll:
                    continue
                # check if empty already exists
                exists = any(o for o in active_col.objects
                             if o.type=='EMPTY' and o.instance_collection==coll)
                if not exists:
                    empty = bpy.data.objects.new(name=f"{coll_name}_instance", object_data=None)
                    empty.instance_type = 'COLLECTION'
                    empty.instance_collection = coll
                    empty.rotation_mode = 'QUATERNION'
                    active_col.objects.link(empty)
                    # restore transform if known
                    tr = info.get('transforms', {}).get(coll_name, {})
                    empty.location = tr.get('location', (0,0,0))
                    empty.rotation_quaternion = tr.get('rotation', (1,0,0,0))
                    empty.scale = tr.get('scale', (1,1,1))

        self.report({'INFO'}, f"Deleted: {name}")
        force_viewport_refresh()
        return {'FINISHED'}

class LINKEDITOR_OT_toggle_expand(bpy.types.Operator):
    """Toggle the expanded state of a library in the UI."""
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

        # Check if the library is unloaded
        orig_norm = normalize_filepath(self.original_filepath)
        if orig_norm in link_active_states and not link_active_states[orig_norm]:
            self.report({'WARNING'}, "Turn visibility ON for switching resolution")
            return {'CANCELLED'}

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

        tgt_fp = normalize_filepath(tgt)
        linked_elements[tgt_fp] = get_linked_item_names(lib)
        if linked_elements[tgt_fp].get('type') == 'collections':
            for coll_name in linked_elements[tgt_fp].get('collections', []):
                if coll_name in transforms:
                    for obj in col.objects:
                        if obj.type == 'EMPTY' and obj.instance_collection and obj.instance_collection.name == coll_name:
                            obj.rotation_mode = 'QUATERNION'
                            obj.location = transforms[coll_name].get('location', [0, 0, 0])
                            obj.rotation_quaternion = transforms[coll_name].get('rotation', [1, 0, 0, 0])
                            obj.scale = transforms[coll_name].get('scale', [1, 1, 1])
                            break

        rs = resolution_status.setdefault(hi_fp, {"high_path": hi_fp, "low_path": lo_fp})
        rs["status"] = "high" if tgt == hi_fp else "low"

        tgt_norm = normalize_filepath(tgt)
        if orig_norm in library_order:
            idx = library_order.index(orig_norm)
            library_order[idx] = tgt_norm

        if orig_norm in link_active_states:
            link_active_states[tgt_norm] = link_active_states.pop(orig_norm)

        force_viewport_refresh()
        return {'FINISHED'}

# ### UI Panel
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
        library_order[:] = [fp for fp in library_order if base(fp) in bases_in_scene or fp in link_active_states]
        for fp in current_norm:
            if base(fp) not in {base(k) for k in library_order}:
                library_order.append(fp)

        for fp in library_order:
            live_fp = next((c for c in current_norm if base(c) == base(fp)), fp)
            abs_fp = bpy.path.abspath(live_fp)
            if live_fp in ephemeral_hidden_libraries or resolution_status.get(live_fp, {}).get("hidden"):
                continue

            expanded = expanded_states.get(live_fp, False)
            row = layout.row(align=True)
            row.operator("linkeditor.toggle_expand", text="",
                         icon="TRIA_DOWN" if expanded else "TRIA_RIGHT",
                         emboss=False).filepath = live_fp
            row.label(text=os.path.basename(abs_fp))
            is_loaded = link_active_states.get(live_fp, True)
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

# ### Registration
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
    except Exception:
        pass
    register()
