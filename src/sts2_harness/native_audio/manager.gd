extends Node

const SCENES: Dictionary[String, String] = {
    "game": "res://harness_audio/game.tscn",
    "run": "res://scenes/run.tscn",
}

var _directory: String

func _init() -> void:
    _directory = (get_script() as Script).resource_path.get_base_dir()
    if "--harness-prepare-audio" in OS.get_cmdline_user_args():
        return
    if not ProjectSettings.load_resource_pack(_directory + "/overlay.pck", true):
        push_error("The headless audio overlay could not be mounted.")
        (Engine.get_main_loop() as SceneTree).quit(2)
        return
    print("HARNESS_AUDIO_OVERLAY_MOUNTED")

func prepare() -> bool:
    if ProjectSettings.get_setting("Fmod/General/auto_initialize", true) != false:
        return _error("FMOD automatic initialization is enabled.")
    var game := FileAccess.get_file_as_string("res://scenes/game.tscn")
    var run := FileAccess.get_file_as_string("res://scenes/run.tscn")
    game = _replace_script(game, "audio_manager_proxy", "audio_proxy")
    run = _replace_script(run, "music_controller_proxy", "music_proxy")
    if game.is_empty() or run.is_empty():
        return false
    game = _remove_node(game, "FmodBankLoader")
    if game.is_empty():
        return false
    game = _remove_node(game, "FmodListener2D")
    if game.is_empty():
        return false
    var pack := PCKPacker.new()
    if not _check(pack.pck_start(_directory + "/overlay.pck"), "Create audio overlay"):
        return false
    for name: String in ["game", "run"]:
        var path := _directory + "/" + name + ".tscn"
        var file := FileAccess.open(path, FileAccess.WRITE)
        if file == null:
            return _error("Cannot write audio scene: " + error_string(FileAccess.get_open_error()))
        file.store_string(game if name == "game" else run)
        var result := file.get_error()
        file.close()
        if not _check(result, "Write audio scene"):
            return false
        if not _check(pack.add_file(SCENES[name], path), "Pack audio scene"):
            return false
    for name: String in ["audio_proxy", "music_proxy"]:
        if not _check(pack.add_file("res://harness_audio/" + name + ".gd", _directory + "/" + name + ".gd"), "Pack audio proxy"):
            return false
    if not _check(pack.flush(), "Finish audio overlay"):
        return false
    if not ProjectSettings.load_resource_pack(_directory + "/overlay.pck", true):
        return _error("Cannot verify the audio overlay.")
    for name: String in ["game", "run"]:
        var scene := ResourceLoader.load(SCENES[name], "PackedScene", ResourceLoader.CACHE_MODE_IGNORE) as PackedScene
        if scene == null:
            return _error("Cannot load the prepared audio scene: " + name)
        var state := scene.get_state()
        var proxy_found := false
        for index: int in state.get_node_count():
            if String(state.get_node_type(index)).begins_with("Fmod"):
                return _error("A native FMOD node remains in the audio scene.")
            for property: int in state.get_node_property_count(index):
                if state.get_node_property_name(index, property) != &"script":
                    continue
                var script := state.get_node_property_value(index, property) as Script
                if script != null and script.resource_path.begins_with("res://harness_audio/"):
                    proxy_found = script.can_instantiate()
        if not proxy_found:
            return _error("The prepared audio proxy is unavailable: " + name)
    return true

func _replace_script(source: String, original: String, replacement: String) -> String:
    var expression := RegEx.new()
    expression.compile('(?m)^\\[ext_resource[^\\n]*path="res://src/gdscript/' + original + '\\.gd"[^\\n]*$')
    var found := expression.search_all(source)
    if found.size() != 1:
        _error("Expected one audio script resource: " + original)
        return ""
    var row := found[0].get_string()
    expression.compile(' id="([^"]+)"')
    var identifier := expression.search(row)
    if identifier == null:
        _error("The audio script resource has no identifier.")
        return ""
    return source.replace(row, '[ext_resource type="Script" path="res://harness_audio/' + replacement + '.gd" id="' + identifier.get_string(1) + '"]')

func _remove_node(source: String, kind: String) -> String:
    var expression := RegEx.new()
    expression.compile('(?ms)^\\[node [^\\n]*type="' + kind + '"[^\\n]*\\n.*?(?=^\\[node |\\z)')
    if expression.search_all(source).size() != 1:
        _error("Expected one audio node: " + kind)
        return ""
    return expression.sub(source, "", true)

func _check(result: Error, operation: String) -> bool:
    if result == OK:
        return true
    return _error(operation + ": " + error_string(result))

func _error(message: String) -> bool:
    push_error(message)
    return false
