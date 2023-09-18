"""
This plugin is a "dependency", see https://packagecontrol.io/docs/dependencies.

It does the following:

- Installs and periodically upgrades Tree-sitter Python bindings, see https://github.com/tree-sitter/py-tree-sitter
    - Importable by other plugins with `import tree_sitter`
- Installs and builds TS languages, e.g. https://github.com/tree-sitter/tree-sitter-python, based on settings
    - Updates languages on command
- Provides APIs for:
    - Getting TS tree by its buffer id
    - Subscribing to tree changes in real time using `sublime_plugin.EventListener`
    - Walking a tree, querying a tree, etc
    - Parsing a string to get a tree, or editing a tree

It's easy to build TS plugins on top of this one, for "structural" editing, selection, navigation, code folding, code
maps… See e.g. https://zed.dev/blog/syntax-aware-editing for ideas. It's performant and never blocks the main thread.

It has the following limitations:

- It doesn't support nested syntax trees, e.g. `<script>` tags in HTML docs
- Due to limitations in Sublime's bundled Python, it requires an external Python 3.8 executable (see settings)
- Due to how syntax highlighting works in Sublime, it can't be used for syntax highlighting
    - See e.g. https://github.com/sublimehq/sublime_text/issues/817
"""

from __future__ import annotations

import os
import subprocess
import time
from threading import Thread
from typing import List, TypedDict, cast

import sublime
import sublime_plugin
from sublime import View

from .src.utils import (
    BUILD_PATH,
    DEPS_PATH,
    LANGUAGE_NAME_TO_PATH,
    LANGUAGE_NAME_TO_REPO,
    LANGUAGE_NAME_TO_SCOPES,
    PROJECT_ROOT,
    ScopeType,
    add_path,
    log,
)

PROJECT_REPO = "https://github.com/sublime-treesitter/treesitter"
SETTINGS_FILENAME = "TreeSitter.sublime-settings"
PYTHON_PATH = "/Users/kyle/.pyenv/versions/3.8.13/bin/python"
# If PIP_PATH isn't set, infer it from PYTHON_PATH
PIP_PATH = "/Users/kyle/.pyenv/versions/3.8.13/bin/pip"

#
# Code for installing tree sitter, and installing/building languages
#


def install_tree_sitter(pip_path: str = PIP_PATH):
    """
    We use a pip 3.8 executable to install tree_sitter wheel. Call with `check=True` to block until subprocess
    completes.
    """
    subprocess.run([pip_path, "install", "--target", str(DEPS_PATH), "tree_sitter"], check=True)


add_path(str(DEPS_PATH))
try:
    # This is a fast way to check if tree_sitter bindings installed
    from tree_sitter import Language, Parser, Tree
except ImportError:
    install_tree_sitter()
    from tree_sitter import Language, Parser, Tree

log(f'Python bindings installed at "{DEPS_PATH}"')
log(f'language repos and .so files installed at "{BUILD_PATH}"')


def get_settings():
    """
    Note that during plugin startup, plugins can't call most `sublime` methods, including `load_settings`.

    [See more here](https://www.sublimetext.com/docs/api_reference.html#plugin-lifecycle).
    """
    return sublime.load_settings("TreeSitter.sublime-settings")


def clone_language(org_and_repo: str):
    _, repo = org_and_repo.split("/")
    subprocess.run(["git", "clone", f"https://github.com/{org_and_repo}", str(BUILD_PATH / repo)], check=True)


def get_so_file(language_name: str):
    return f"language-{language_name}.so"


def clone_languages():
    """
    Clone language repos from which language `.so` files can be built.
    """
    language_names = cast(List[str], get_settings().get("installed_languages"))
    files = set(f for f in os.listdir(BUILD_PATH))
    for name in set(language_names):
        if name not in LANGUAGE_NAME_TO_REPO:
            log(f'"{name}" language is not supported, read more at {PROJECT_REPO}')
            continue

        org_and_repo = LANGUAGE_NAME_TO_REPO[name]
        _, repo = org_and_repo.split("/")
        if repo in files:
            # We've already cloned this repo
            continue

        log(f"installing {org_and_repo} repo for {name} language", with_status=True)
        clone_language(org_and_repo)
        files.add(repo)  # Avoid cloning a repo used for multiple languages multiple times


def build_languages():
    """
    Build missing language `.so` files for installed languages. We use python 3.8 executable to build languages,
    because the python bundled with Sublime can't do this.

    Note: `installed_languages` specified in `TreeSitter.sublime-settings`, `python` installed by default.
    """
    language_names = cast(List[str], get_settings().get("installed_languages"))
    files = set(f for f in os.listdir(BUILD_PATH))
    for name in set(language_names):
        if (so_file := get_so_file(name)) in files:
            # We've already built this .so file
            continue

        if name not in LANGUAGE_NAME_TO_PATH:
            continue

        path = LANGUAGE_NAME_TO_PATH[name]
        log(f"building {name} language from files at {path}", with_status=True)
        subprocess.run(
            [
                PYTHON_PATH,
                str(PROJECT_ROOT / "src" / "build.py"),
                str(BUILD_PATH / so_file),
                str(BUILD_PATH / path),
            ],
            check=True,
        )


def instantiate_languages():
    """
    Instantiate `Language`s for language `.so` files, and put them in `SCOPE_TO_LANGUAGE`.
    """
    language_names = cast(List[str], get_settings().get("installed_languages"))
    files = set(f for f in os.listdir(BUILD_PATH))
    for name in set(language_names):
        if name not in LANGUAGE_NAME_TO_SCOPES:
            continue

        if (so_file := get_so_file(name)) not in files:
            continue

        language = Language(str(BUILD_PATH / so_file), name)

        for scope in LANGUAGE_NAME_TO_SCOPES[name]:
            SCOPE_TO_LANGUAGE[scope] = language


#
# Code for caching syntax trees by their `buffer_id`s, and keeping them in sync as `TextChange`s occur
# https://www.sublimetext.com/docs/api_reference.html#sublime.View
#


def check_scope(scope: str | None):
    if not scope or scope not in SCOPE_TO_LANGUAGE:
        return None
    return scope


def edit(parser: Parser, scope: ScopeType, change: sublime.TextChange, tree: Tree) -> Tree:
    """
    Note: the `set_language` call costs nothing, I can call it ~2m times a second on 2021 M1 MPB with 16gb RAM.
    """
    parser.set_language(SCOPE_TO_LANGUAGE[scope])
    return tree


def parse(parser: Parser, scope: ScopeType, s: str) -> Tree:
    parser.set_language(SCOPE_TO_LANGUAGE[scope])
    return parser.parse(s.encode())


class TreeDict(TypedDict):
    tree: Tree
    updated_s: float


def make_tree_dict(tree: Tree) -> TreeDict:
    return {"tree": tree, "updated_s": time.monotonic()}


def get_scope(view: View) -> ScopeType | None:
    syntax = view.syntax()
    if not syntax:
        return None
    return cast(ScopeType, syntax.scope)


def publish_tree_update(window: sublime.Window | None, buffer_id: int, scope: str):
    if not window:
        return

    window.run_command(
        "tree_sitter_update_tree",
        {
            "buffer_id": buffer_id,
            "scope": scope or "",
        },
    )


MAX_CACHED_TREES = 32
SCOPE_TO_LANGUAGE: dict[ScopeType, Language] = {}

# LRU cache, dict of `(buffer_id, syntax)` tuple keys pointing to dict with tree instance and other metadata.
BUFFER_ID_TO_TREE: dict[int, TreeDict] = {}


def trim_cached_trees(size: int = MAX_CACHED_TREES):
    """
    Note that trimming an item is O(N) in `MAX_CACHED_TREES`.

    This is fast enough, and much easier than using heapq or similar to implement a sorted set.
    """
    while len(BUFFER_ID_TO_TREE) > MAX_CACHED_TREES:
        _, buffer_id = min((d["updated_s"], buffer_id) for buffer_id, d in BUFFER_ID_TO_TREE.items())
        BUFFER_ID_TO_TREE.pop(buffer_id, None)


def parse_view(parser: Parser, view: View, publish_update: bool = True):
    """
    Defined outside `TreeSitterEventListener` so it can be called by anything, e.g. called on the active buffer after a
    new language is installed and loaded.
    """
    syntax = view.syntax()
    scope = syntax and syntax.scope
    if not (scope := check_scope(scope)):
        return

    buffer_id = view.buffer().id()
    tree = parse(parser, scope, s=view.substr(sublime.Region(0, view.size())))

    BUFFER_ID_TO_TREE[buffer_id] = make_tree_dict(tree)

    if publish_update:
        publish_tree_update(view.window(), buffer_id=buffer_id, scope=scope)
    trim_cached_trees()


def load_languages():
    """
    Defined as a function so it can all be run in a thread on `plugin_loaded`.
    """
    clone_languages()
    build_languages()
    instantiate_languages()
    if view := sublime.active_window().active_view():
        if view.buffer().id() not in BUFFER_ID_TO_TREE:
            parse_view(Parser(), view, publish_update=False)


def plugin_loaded():
    """
    Called after plugin is loaded (we can use functions like `sublime.load_settings`), but before events fired.

    We load any uncloned or unbuilt languages in the background, and if a language needed to parse the active view was
    just installed, we parse this view when we're finished.

    Note that "publishing update" with `window.run_command` in `plugin_loaded` leads to noisy but unimportant `Error
    rewriting command`, see https://github.com/sublimelsp/LSP/pull/2277 for more info.
    """
    instantiate_languages()
    Thread(target=load_languages).start()


class TreeSitterUpdateTreeCommand(sublime_plugin.WindowCommand):
    """
    So client code can "subscribe" to tree updates with an `EventListener`. For example:

    ```py
    class Listener(sublime_plugin.EventListener):
        def on_window_command(self, window, command, args):
            print(command, args["buffer_id"])
    ```
    """

    def run(self, **kwargs):
        pass


class TreeSitterEventListener(sublime_plugin.EventListener):
    """
    One of these for the whole Sublime instance.

    When a buffer is loaded, reverted, or reloaded, we do a full parse to get its tree, and cache that. This ensures the
    tree matches the buffer text even if this text is edited e.g. outside of ST.
    """

    def __init__(self):
        super().__init__()
        self.parser = Parser()

    def handle_load(self, view: View):
        parse_view(self.parser, view)

    def on_activated_async(self, view: View):
        """
        Ensure that we parse buffers on Sublime Text startup, where `on_load` callbacks not called. Testing shows that
        `on_text_changed` callbacks always enqueued after `on_activated` callbacks.
        """
        if view.buffer().id() not in BUFFER_ID_TO_TREE:
            self.handle_load(view)

    def on_load_async(self, view: View):
        """
        Testing suggests that `on_activated_async` always called before `on_load_async`. To be extra safe, we handle
        both of these events, and bail out if the other has already run for a given buffer.
        """
        if view.buffer().id() not in BUFFER_ID_TO_TREE:
            self.handle_load(view)

    def on_reload_async(self, view: View):
        self.handle_load(view)

    def on_revert_async(self, view: View):
        self.handle_load(view)


class TreeSitterTextChangeListener(sublime_plugin.TextChangeListener):
    """
    Under the hood, ST synchronously puts any async callbacks onto a queue. It asynchronously handles them in FIFO
    order in a separate thread. All async callbacks are handled by the same thread. Source code suggests this,
    testing with `time.sleep` confirms it. This ensures there are no races between "text change" events
    (almost always edit) and "load" (always parse).

    When a text change occurs, we get its buffer and its syntax, look up the tree and metadata, and update/create the
    tree as necessary. Every listener instance is bound to a buffer, so we know in which buffer text changes occur.
    """

    def __init__(self):
        super().__init__()
        self.parser = Parser()

    def on_text_changed_async(self, changes: list[sublime.TextChange]):
        view = self.buffer.primary_view()
        syntax = view.syntax()
        scope = syntax and syntax.scope
        if not (scope := check_scope(scope)):
            return

        buffer_id = self.buffer.id()

        for change in changes:
            if buffer_id not in BUFFER_ID_TO_TREE:
                tree = parse(self.parser, scope, s=view.substr(sublime.Region(0, view.size())))
            else:
                tree = edit(self.parser, scope, change, BUFFER_ID_TO_TREE[buffer_id]["tree"])

            BUFFER_ID_TO_TREE[buffer_id] = make_tree_dict(tree)

        # May as well handle all changes before "publishing" update
        publish_tree_update(view.window(), buffer_id=buffer_id, scope=scope)
        trim_cached_trees()
