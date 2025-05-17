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
library_order                = []
expanded_states              = {}
link_active_states           = {}
linked_elements              = {}
resolution_status            = {}
ephemerally_loaded_libraries = set()
ephemeral_hidden_libraries   = set()
_RENDER_SWAPS = {}

# ────────────────────────────────────────────────────────────────────
#  Helpers
# ────────────────────────────────────────────────────────────────────

def normalize_filepath(filepath):
    """Return Blender‑style forward‑slash path (relative if prefs allow)."""
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
    """Redraw every 3‑D viewport in every Blender window."""
    bpy.context.view_layer.update()
    wm = bpy.context.window_manager
    if not wm:
        return
    for win in wm.windows:
        for area in win.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


def reload_library(lib):
    """
    Version‑safe wrapper for Library.reload().
    Blender 4.2 accepts no arguments; older alphas need do_remap=True.
    """
    try:
        lib.reload()
    except RuntimeError:
        lib.reload(do_remap=True)   # fallback for very old builds


# ──────────────────  dynamic low/high‑res helpers  ─────────────────
def lo_suffix() -> str:
    """
    Read the raw suffix from the add-on prefs and ensure it ends in '.blend'.
    """
    prefs = bpy.context.preferences.addons[__name__].preferences
    raw   = (prefs.lo_suffix or "_Lo").strip()
    return raw if raw.lower().endswith(".blend") else raw + ".blend"


def is_lo_file(path: str) -> bool:
    """True if <path> ends with the active low‑res suffix."""
    return normalize_filepath(path).endswith(lo_suffix())


def get_hi_res_path(path: str) -> str:
    """Convert a low‑res path to its hi‑res counterpart, respecting the suffix."""
    p = normalize_filepath(path)
    return p[:-len(lo_suffix())] + ".blend" if is_lo_file(p) else p


def lib_base(path: str) -> str:
    """Strip '.blend' and any low‑res suffix to obtain a library base key."""
    p = normalize_filepath(path)
    if p.endswith(lo_suffix()):
        return p[:-len(lo_suffix())]
    return p[:-6] if p.lower().endswith(".blend") else p


# ────────────────────────────────────────────────────────────────────
#  Linked‑item capture (robust)                                      
# ────────────────────────────────────────────────────────────────────
def get_linked_item_names(library):
    try:
        lib_fp_norm = normalize_filepath(library.filepath)
    except ReferenceError:
        return {}

    result = {}
    for dt in (
        'objects','collections','lights','materials','cameras','meshes',
        'armatures','curves','lattices','metaballs','texts','grease_pencils',
        'images'
    ):
        names = []
        for item in getattr(bpy.data, dt):
            lib = safe_library(item)
            if lib and normalize_filepath(lib.filepath) == lib_fp_norm:
                names.append(item.name)
        if names:
            result[dt] = names
    return result

# ────────────────────────────────────────────────────────────────────
#  Hi‑res loader (hidden)                                            
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
    need_colls  = set()
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
            dst.meshes      = [m for m in src.meshes      if base(m) in need_meshes]
            dst.collections = [c for c in src.collections if base(c) in need_colls]
    except Exception as e:
        print("load_highres_hidden() failed:", e)
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
    """
    Clear all cached link-editor state when a new .blend is loaded,
    so the panel shows only the current file’s libraries.
    """
    library_order.clear()
    expanded_states.clear()
    link_active_states.clear()
    linked_elements.clear()
    resolution_status.clear()
    ephemerally_loaded_libraries.clear()
    ephemeral_hidden_libraries.clear()
    _RENDER_SWAPS.clear()

# ────────────────────────────────────────────────────────────────────
#  Render‑time swapping                                              
# ────────────────────────────────────────────────────────────────────

@persistent
def prepare_render(scene, _):
    for lo_fp, rs in resolution_status.items():
        if not rs.get("high_res_for_render"):
            continue

        hi_fp  = rs["high_path"]
        base   = lib_base(lo_fp)
        lib    = next((l for l in bpy.data.libraries
                       if lib_base(l.filepath) == base), None)
        if not lib or normalize_filepath(lib.filepath) == hi_fp:
            continue

        _RENDER_SWAPS[base] = lib.filepath   # save low‑res path
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
#  Operators (unchanged except for reduced comments)                 
# ────────────────────────────────────────────────────────────────────

class LinkEditorPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    lo_suffix: bpy.props.StringProperty(
        name="Low-res suffix",
        description="Suffix (no .blend) for low-resolution files",
        default="_Lo",
    )

    def draw(self, context):
        layout = self.layout
        layout.label(text="Low-res File Suffix (omit .blend):")
        layout.prop(self, "lo_suffix", text="")

class LINKEDITOR_OT_render_resolution(bpy.types.Operator):
    """Toggle whether this low‑res library is swapped to Hi‑res at render time"""
    bl_idname = "linkeditor.render_resolution"
    bl_label  = "Toggle Render Resolution"

    filepath: bpy.props.StringProperty()

    def execute(self, context):
        lo_fp    = normalize_filepath(self.filepath)
        suffix   = lo_suffix()           # cache once

        if not is_lo_file(lo_fp):
            self.report({'WARNING'}, f"Works only on *{suffix} files.")
            return {'CANCELLED'}

        rs = resolution_status.setdefault(
            lo_fp, {
                "status"             : "low",
                "low_path"           : lo_fp,
                "high_path"          : get_hi_res_path(lo_fp),
                "high_res_for_render": False,
            })

        rs["high_res_for_render"] ^= True
        force_viewport_refresh()

        state = "ON" if rs["high_res_for_render"] else "OFF"
        self.report({'INFO'}, f"Hi‑res render {state}.")
        return {'FINISHED'}



class LINKEDITOR_OT_load_and_unload(bpy.types.Operator):
    """Unload a library if it’s loaded, or re-link it if it was unloaded."""
    bl_idname = "linkeditor.load_and_unload"
    bl_label  = "Load/Unload Linked File"

    filepath: bpy.props.StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)

        # 1) Try to *find* the lib, in which case we unload it.
        lib = next(
            (l for l in bpy.data.libraries
             if normalize_filepath(l.filepath) == fp),
            None
        )
        if lib:
            # capture its items so we can put them back later
            linked_elements[fp] = get_linked_item_names(lib)

            # remove the library from bpy.data
            bpy.data.libraries.remove(lib)
            link_active_states[fp] = False
            return {'FINISHED'}

        # 2) If it wasn’t in bpy.data.libraries, see if we have a cache to reload
        if fp in linked_elements:
            with bpy.data.libraries.load(fp, link=True) as (src, dst):
                for dt, names in linked_elements[fp].items():
                    setattr(dst, dt, [e for e in getattr(src, dt) if e in names])

            # link the newly loaded objects/collections into the active layer
            active_col = context.view_layer.active_layer_collection.collection
            for obj in dst.objects:
                if obj.name not in active_col.objects:
                    active_col.objects.link(obj)
            for coll in dst.collections:
                if coll.name not in active_col.children:
                    active_col.children.link(coll)

            link_active_states[fp] = True
            return {'FINISHED'}

        # 3) Otherwise, nothing to do
        self.report({'WARNING'}, "No library to unload or reload")
        return {'CANCELLED'}


class LINKEDITOR_OT_relocate(bpy.types.Operator, ImportHelper):
    bl_idname = "linkeditor.relocate"
    bl_label  = "Relocate Linked File"
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

class LINKEDITOR_OT_reload(bpy.types.Operator):
    """Reload a linked .blend, preserving only the previously visible items."""
    bl_idname = "linkeditor.reload"
    bl_label  = "Reload Linked File"

    filepath: bpy.props.StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)

        # 1) If the Library is currently loaded, capture its items and remove it
        lib = next(
            (l for l in bpy.data.libraries
             if normalize_filepath(l.filepath) == fp),
            None
        )
        if lib:
            # cache the names of everything linked from that .blend
            linked_elements[fp] = get_linked_item_names(lib)
            try:
                bpy.data.libraries.remove(lib)
            except Exception as e:
                self.report({'ERROR'}, f"Failed to unload before reload: {e}")
                return {'CANCELLED'}

        # 2) Now we should have a cache of names
        items = linked_elements.get(fp)
        if not items:
            self.report({'WARNING'}, "No items found to reload")
            return {'CANCELLED'}

        # 3) Load only those same names from disk
        try:
            with bpy.data.libraries.load(fp, link=True) as (src, dst):
                for dt, names in items.items():
                    setattr(dst, dt, [e for e in getattr(src, dt) if e in names])
        except Exception as e:
            self.report({'ERROR'}, f"Failed to reload library: {e}")
            return {'CANCELLED'}

        # 4) Link them back into the active view layer
        active_col = context.view_layer.active_layer_collection.collection
        for obj in dst.objects:
            if obj.name not in active_col.objects:
                active_col.objects.link(obj)
        for coll in dst.collections:
            if coll.name not in active_col.children:
                active_col.children.link(coll)

        link_active_states[fp] = True
        self.report({'INFO'}, f"Reloaded: {os.path.basename(fp)}")
        return {'FINISHED'}

class LINKEDITOR_OT_remove(bpy.types.Operator):
    """Delete a linked .blend (both hi- and lo-res variants if present)"""
    bl_idname = "linkeditor.remove"
    bl_label  = "Delete Linked File"

    filepath: bpy.props.StringProperty()

    def execute(self, context):
        fp = normalize_filepath(self.filepath)

        # Figure out which is low and which is high
        if is_lo_file(fp):
            lo_fp = fp
            hi_fp = fp[:-len(lo_suffix())] + ".blend"
        else:
            hi_fp = fp
            lo_fp = fp[:-6] + lo_suffix()

        # Candidates for removal
        targets = {lo_fp, hi_fp}

        # 1) Look for the library in bpy.data.libraries
        lib = next(
            (l for l in bpy.data.libraries
             if normalize_filepath(l.filepath) in targets),
            None
        )
        if not lib:
            self.report({'WARNING'}, "Library not found")
            return {'CANCELLED'}

        # Capture its display name before removal
        name = os.path.basename(normalize_filepath(lib.filepath))

        # Remove it
        try:
            bpy.data.libraries.remove(lib)
        except RuntimeError as e:
            self.report({'ERROR'}, f"Could not delete library: {e}")
            return {'CANCELLED'}

        # 2) Clean up any cached state for both hi- and lo-res keys
        for key in targets:
            link_active_states.pop(key,    None)
            linked_elements .pop(key,    None)
            resolution_status.pop(key,    None)

        self.report({'INFO'}, f"Deleted: {name}")
        return {'FINISHED'}



class LINKEDITOR_OT_toggle_expand(bpy.types.Operator):
    bl_idname = "linkeditor.toggle_expand"
    bl_label  = "Toggle Expand"
    filepath: bpy.props.StringProperty()
    def execute(self, _):
        n = normalize_filepath(self.filepath)
        expanded_states[n] = not expanded_states.get(n, False)
        return {'FINISHED'}

class LINKEDITOR_OT_switch_mode(bpy.types.Operator, ImportHelper):
    """Switch between Hi‑res and Low‑res versions of the linked library"""
    bl_idname = "linkeditor.switch_mode"
    bl_label  = "Switch Mode"

    original_filepath: bpy.props.StringProperty()
    filter_glob: bpy.props.StringProperty(default="*.blend", options={'HIDDEN'})

    # decide which file we need next
    def invoke(self, context, _):
        orig  = normalize_filepath(self.original_filepath)
        hi_fp = get_hi_res_path(orig)
        lo_fp = hi_fp[:-6] + lo_suffix() 
        tgt   = lo_fp if orig == hi_fp else hi_fp

        if not os.path.exists(bpy.path.abspath(tgt)):
            context.window_manager.fileselect_add(self)
            return {'RUNNING_MODAL'}

        self.filepath = tgt
        return self.execute(context)

    # flip the path, reload, relink, then prune any hidden duplicates
    def execute(self, context):
        hi_fp = get_hi_res_path(normalize_filepath(self.original_filepath))
        lo_fp = hi_fp[:-6] + lo_suffix()
        tgt   = normalize_filepath(self.filepath)

        lib = next((l for l in bpy.data.libraries
                    if normalize_filepath(l.filepath) in {hi_fp, lo_fp}), None)
        if not lib:
            self.report({'ERROR'}, "Linked library not found")
            return {'CANCELLED'}

        # leaving Hi‑res → remember elements
        if normalize_filepath(lib.filepath) == hi_fp:
            linked_elements[hi_fp] = get_linked_item_names(lib)

        # if about to load Hi‑res, drop the hidden render‑only copy
        if tgt == hi_fp:
            hid = next((h for h in ephemerally_loaded_libraries
                        if normalize_filepath(h.filepath) == hi_fp), None)
            if hid:
                bpy.data.libraries.remove(hid)
                ephemerally_loaded_libraries.discard(hid)
            ephemeral_hidden_libraries.discard(hi_fp)

        # switch file path and reload
        lib.filepath = tgt
        lib.reload()

        # ensure objects/collections are in the current view‑layer collection
        col = context.view_layer.active_layer_collection.collection
        for obj in bpy.data.objects:
            if obj.library == lib and obj.name not in col.objects:
                col.objects.link(obj)
        for coll in bpy.data.collections:
            if coll.library == lib and coll.name not in col.children:
                col.children.link(coll)

        # update resolution status
        rs = resolution_status.setdefault(hi_fp, {"high_path": hi_fp,
                                                 "low_path" : lo_fp})
        rs["status"] = "high" if tgt == hi_fp else "low"

        # refresh saved list after loading Hi‑res
        if tgt == hi_fp:
            linked_elements[hi_fp] = get_linked_item_names(lib)

        # ──────────────────────────────────────────────
        #  NEW: prune hidden hi‑res libraries that are
        #       now redundant to save RAM.
        # ──────────────────────────────────────────────
        for hlib in list(ephemerally_loaded_libraries):
            if normalize_filepath(hlib.filepath) == hi_fp and hlib is not lib:
                try:
                    bpy.data.libraries.remove(hlib)
                except RuntimeError:
                    pass
                ephemerally_loaded_libraries.discard(hlib)
                ephemeral_hidden_libraries.discard(hi_fp)
        # ──────────────────────────────────────────────

        force_viewport_refresh()
        return {'FINISHED'}


# ────────────────────────────────────────────────────────────────────
#  UI Panel                                                           
# ────────────────────────────────────────────────────────────────────
class LINKEDITOR_PT_panel(bpy.types.Panel):
    bl_label       = "Link Manager"
    bl_idname      = "LINKEDITOR_PT_panel"
    bl_space_type  = "VIEW_3D"
    bl_region_type = "UI"
    bl_category    = "Link Manager"

    def draw(self, context):
        scn    = context.scene
        layout = self.layout
        base   = get_hi_res_path            # helper already honours custom suffix

        # ── editable suffix field (title above the box) ─────────────
        col = layout.column(align=True)
        col.label(text="Lo‑res file suffix (Ex: foo_Lo.blend, write _Lo")
        wm = context.window_manager
        prefs = context.preferences.addons[__name__].preferences
        col.prop(prefs, "lo_suffix", text="")

        layout.separator()
        layout.label(text="Linked Files:")

        # ── cache normalised paths once for speed ───────────────────
        current_norm   = [normalize_filepath(l.filepath) for l in bpy.data.libraries]
        bases_in_scene = {base(fp) for fp in current_norm}

        # keep library_order unique by base path
        for fp in current_norm:
            if all(base(fp) != base(k) for k in library_order):
                library_order.append(fp)

        show = [fp for fp in library_order
                if base(fp) in bases_in_scene or fp in link_active_states]

        for fp in show:
            live_fp = next((c for c in current_norm if base(c) == base(fp)), fp)
            abs_fp  = bpy.path.abspath(live_fp)

            if live_fp in ephemeral_hidden_libraries:
                continue
            if resolution_status.get(live_fp, {}).get("hidden"):
                continue

            expanded = expanded_states.get(base(fp), False)
            row = layout.row(align=True)

            # expand / collapse triangle
            row.operator("linkeditor.toggle_expand", text="",
                         icon="TRIA_DOWN" if expanded else "TRIA_RIGHT",
                         emboss=False).filepath = base(fp)

            row.label(text=os.path.basename(abs_fp))

            # load / unload eye icon
            is_loaded = link_active_states.get(base(fp), True)
            row.operator("linkeditor.load_and_unload", text="",
                         icon="HIDE_OFF" if is_loaded else "HIDE_ON"
                       ).filepath = live_fp

            # switch mode (low <-> high)
            is_lo = is_lo_file(abs_fp)
            row.operator("linkeditor.switch_mode", text="",
                         icon="SPLIT_HORIZONTAL" if is_lo else "VIEW_ORTHO"
                       ).original_filepath = live_fp

            # hi‑res‑for‑render toggle (camera icon)
            if is_lo:
                hi_r = resolution_status.get(live_fp, {}).get("high_res_for_render", False)
                row.operator("linkeditor.render_resolution", text="",
                             icon="ANTIALIASED" if hi_r else "ALIASED"
                           ).filepath = live_fp
            else:
                row.label(text="", icon="ANTIALIASED")

            # relocate, reload, remove
            row.operator("linkeditor.relocate", text="", icon="GRAPH"
                       ).original_filepath = live_fp
            row.operator("linkeditor.reload",   text="", icon="FILE_REFRESH"
                       ).filepath          = live_fp
            row.operator("linkeditor.remove",   text="", icon="X"
                       ).filepath          = live_fp

            # optional expanded path line
            if expanded:
                layout.row().label(text=live_fp)

        layout.separator()
        layout.operator("wm.link", text="Add Link", icon="ADD")






# ────────────────────────────────────────────────────────────────────
#  Registration                                                       
# ────────────────────────────────────────────────────────────────────
classes = (
    LINKEDITOR_OT_toggle_expand, LINKEDITOR_OT_load_and_unload, LINKEDITOR_OT_relocate,
    LINKEDITOR_OT_reload,        LINKEDITOR_OT_remove,          LINKEDITOR_OT_switch_mode,
    LINKEDITOR_OT_render_resolution, LINKEDITOR_PT_panel,
)

def register():
    bpy.utils.register_class(LinkEditorPreferences)
    # put the suffix on WM instead of Scene
    bpy.types.WindowManager.linkeditor_lo_suffix = bpy.props.StringProperty(
        name        = "Lo-res suffix",
        description = "Suffix (no .blend) for low-res files",
        default     = "_Lo",
    )
    if linkeditor_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(linkeditor_load_post)
    if prepare_render not in bpy.app.handlers.render_pre:
        bpy.app.handlers.render_pre.append(prepare_render)
    if restore_render not in bpy.app.handlers.render_post:
        bpy.app.handlers.render_post.append(restore_render)
        bpy.app.handlers.render_cancel.append(restore_render)
    for c in classes:
        bpy.utils.register_class(c)

def unregister():
    bpy.utils.unregister_class(LinkEditorPreferences)
    # remove it from WM
    del bpy.types.WindowManager.linkeditor_lo_suffix
    if linkeditor_load_post in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(linkeditor_load_post)

    for h in ("render_pre", "render_post", "render_cancel"):
        lst = getattr(bpy.app.handlers, h)
        if prepare_render in lst:    lst.remove(prepare_render)
        if restore_render in lst:    lst.remove(restore_render)
    for c in reversed(classes):
        bpy.utils.unregister_class(c)


if __name__ == "__main__":
    try:
        unregister()
    except Exception:
        pass
    register()