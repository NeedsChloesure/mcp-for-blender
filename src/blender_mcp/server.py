# blender_mcp_server.py
from mcp.server.fastmcp import FastMCP, Context
import argparse
import socket
import json
import logging
import tempfile
import threading
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Any
import os
import sys
import time
import base64
import re

from .image_files import deliver_image, image_output_mode
from .addon_manager import (
    handshake_addon,
    format_handshake_log,
    run_cli as run_addon_cli,
    EXPECTED_ADDON_PROTOCOL_VERSION,
    check_addon_status_on_startup,
)
<<<<<<< HEAD
=======
from .consent_prompt import maybe_prompt_for_consent
from .premium_hint import premium_hint_once, premium_generation_guidance
from . import blender_scripts, context_log, generation
>>>>>>> upstream/main
from .safe_mode import safe_mode_enabled, validate_code, SandboxViolation, SAFE_MODE_ENV
from .openai_apps import (
    APP_MIME_TYPE,
    VIEWPORT_STATE_META,
    VIEWPORT_TITLE,
    VIEWPORT_URI,
    PickerOption,
    client_extensions,
    is_app_only,
    pick_asset,
    picked_reply,
    supports_apps,
    supports_openai_forms,
    viewport_html,
    viewport_icon,
    viewport_store,
)
from mcp.types import CallToolResult, ImageContent, ResourceLink, TextContent, ToolAnnotations
from urllib.parse import quote, unquote

# Configure logging
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BlenderMCPServer")

# Default configuration
DEFAULT_HOST = "localhost"
DEFAULT_PORT = 9876


def parse_connection_args(argv):
    """Parse --host/--port out of argv, ignoring anything else.

    parse_known_args is deliberate: MCP clients sometimes append their own
    arguments to the server command, and an unrecognised one must not abort
    startup. Unknown args are logged rather than dropped silently, so a typo
    like --prot does not masquerade as "connected to the default port".
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args, unknown = parser.parse_known_args(argv)
    if unknown:
        logger.warning(f"Ignoring unrecognized command-line arguments: {unknown}")
    return args.host, args.port


def resolve_connection(cli_host=None, cli_port=None):
    """Resolve the Blender address: CLI flags > environment > defaults."""
    host = cli_host or os.getenv("BLENDER_HOST", DEFAULT_HOST)

    if cli_port is not None:
        return host, cli_port

    raw_port = os.getenv("BLENDER_PORT")
    if raw_port is None or raw_port == "":
        return host, DEFAULT_PORT
    try:
        return host, int(raw_port)
    except ValueError:
        logger.warning(
            f"BLENDER_PORT={raw_port!r} is not a valid port number; "
            f"falling back to {DEFAULT_PORT}"
        )
        return host, DEFAULT_PORT


# Set from --host/--port in main(); these take precedence over the
# BLENDER_HOST/BLENDER_PORT environment variables.
CLI_HOST = None
CLI_PORT = None

_addon_handshake = None
_addon_handshake_checked = False
_addon_handshake_lock = threading.Lock()

@dataclass
class BlenderConnection:
    host: str
    port: int
    sock: socket.socket = None  # Changed from 'socket' to 'sock' to avoid naming conflict
    # Serializes send+receive so two commands can never interleave on one socket.
    # Without this, a second command's response can be read as the first's, and
    # the stream stays desynced until the 180s timeout fires.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def connect(self) -> bool:
        """Connect to the Blender addon socket server"""
        if self.sock:
            return True
            
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.sock.connect((self.host, self.port))
            logger.info(f"Connected to Blender at {self.host}:{self.port}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to Blender: {str(e)}")
            self.sock = None
            return False
    
    def disconnect(self):
        """Disconnect from the Blender addon"""
        if self.sock:
            try:
                self.sock.close()
            except Exception as e:
                logger.error(f"Error disconnecting from Blender: {str(e)}")
            finally:
                self.sock = None

    def receive_full_response(self, sock, buffer_size=8192):
        """Receive the complete response, potentially in multiple chunks"""
        chunks = []
        # Use a consistent timeout value that matches the addon's timeout
        sock.settimeout(180.0)  # Match the addon's timeout
        
        try:
            while True:
                try:
                    chunk = sock.recv(buffer_size)
                    if not chunk:
                        # If we get an empty chunk, the connection might be closed
                        if not chunks:  # If we haven't received anything yet, this is an error
                            raise Exception("Connection closed before receiving any data")
                        break
                    
                    chunks.append(chunk)
                    
                    # Check if we've received a complete JSON object
                    try:
                        data = b''.join(chunks)
                        json.loads(data.decode('utf-8'))
                        # If we get here, it parsed successfully
                        logger.info(f"Received complete response ({len(data)} bytes)")
                        return data
                    except json.JSONDecodeError:
                        # Incomplete JSON, continue receiving
                        continue
                except socket.timeout:
                    # If we hit a timeout during receiving, break the loop and try to use what we have
                    logger.warning("Socket timeout during chunked receive")
                    break
                except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
                    logger.error(f"Socket connection error during receive: {str(e)}")
                    raise  # Re-raise to be handled by the caller
        except socket.timeout:
            logger.warning("Socket timeout during chunked receive")
        except Exception as e:
            logger.error(f"Error during receive: {str(e)}")
            raise
            
        # If we get here, we either timed out or broke out of the loop
        # Try to use what we have
        if chunks:
            data = b''.join(chunks)
            logger.info(f"Returning data after receive completion ({len(data)} bytes)")
            try:
                # Try to parse what we have
                json.loads(data.decode('utf-8'))
                return data
            except json.JSONDecodeError:
                # If we can't parse it, it's incomplete
                raise Exception("Incomplete JSON response received")
        else:
            raise Exception("No data received")

    def send_command(self, command_type: str, params: Dict[str, Any] = None, read_only: bool = False) -> Dict[str, Any]:
        """Send a command to Blender and return the response.

        `read_only` marks an execute_code the server runs only to observe the
        scene, so the Viewport app doesn't treat it as an edit and recapture.
        """
        # Hold the lock across send+receive: the response is matched to the
        # command purely by ordering on the stream, so overlapping calls would
        # hand each other's responses back.
        with self._lock:
            # The Viewport app watches these to recapture once Blender goes quiet.
            viewport_store.command_started()
            try:
                return self._send_command_locked(command_type, params)
            finally:
<<<<<<< HEAD
                viewport_store.command_finished(command_type)
=======
                viewport_store.command_finished("observe" if read_only else command_type)
>>>>>>> upstream/main

    def _send_command_locked(self, command_type: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        if not self.sock and not self.connect():
            raise ConnectionError("Not connected to Blender")

        command = {
            "type": command_type,
            "params": params or {}
        }

        try:
            # Log the command being sent
            logger.info(f"Sending command: {command_type} with params: {params}")
            
            # Send the command
            self.sock.sendall(json.dumps(command).encode('utf-8'))
            logger.info(f"Command sent, waiting for response...")
            
            # Set a timeout for receiving - use the same timeout as in receive_full_response
            self.sock.settimeout(180.0)  # Match the addon's timeout
            
            # Receive the response using the improved receive_full_response method
            response_data = self.receive_full_response(self.sock)
            logger.info(f"Received {len(response_data)} bytes of data")
            
            response = json.loads(response_data.decode('utf-8'))
            logger.info(f"Response parsed, status: {response.get('status', 'unknown')}")
            
            if response.get("status") == "error":
                logger.error(f"Blender error: {response.get('message')}")
                raise Exception(response.get("message", "Unknown error from Blender"))
            
            return response.get("result", {})
        except socket.timeout:
            logger.error("Socket timeout while waiting for response from Blender")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            # Just invalidate the current socket so it will be recreated next time
            self.sock = None
            raise Exception("Timeout waiting for Blender response - try simplifying your request. If Blender is running headless (blender -b), commands never execute; run Blender with a GUI or via 'xvfb-run -a blender' instead")
        except (ConnectionError, BrokenPipeError, ConnectionResetError) as e:
            logger.error(f"Socket connection error: {str(e)}")
            self.sock = None
            raise Exception(f"Connection to Blender lost: {str(e)}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON response from Blender: {str(e)}")
            # Try to log what was received
            if 'response_data' in locals() and response_data:
                logger.error(f"Raw response (first 200 bytes): {response_data[:200]}")
            raise Exception(f"Invalid response from Blender: {str(e)}")
        except Exception as e:
            logger.error(f"Error communicating with Blender: {str(e)}")
            # Don't try to reconnect here - let the get_blender_connection handle reconnection
            self.sock = None
            raise Exception(f"Communication error with Blender: {str(e)}")

@asynccontextmanager
async def server_lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Manage server startup and shutdown lifecycle"""
    # We don't need to create a connection here since we're using the global connection
    # for resources and tools

    try:
        # Just log that we're starting up
        logger.info("BlenderMCP server starting up")

        try:
            status = check_addon_status_on_startup()
            if status.needs_action:
                logger.warning(status.message)
            elif status.message:
                logger.info(status.message)
        except Exception as e:
            logger.debug(f"Addon status check skipped: {e}")

        # Try to connect to Blender on startup to verify it's available
        try:
            # This will initialize the global connection if needed
            blender = get_blender_connection()
            logger.info("Successfully connected to Blender on startup")
            if _addon_handshake and not _addon_handshake.up_to_date:
                logger.warning(format_handshake_log(_addon_handshake))
        except Exception as e:
            logger.warning(f"Could not connect to Blender on startup: {str(e)}")
            logger.warning("Make sure the Blender addon is running before using Blender resources or tools")

        # Return an empty context - we're using the global connection
        yield {}
    finally:
        # Clean up the global connection on shutdown
        global _blender_connection
        if _blender_connection:
            logger.info("Disconnecting from Blender on shutdown")
            _blender_connection.disconnect()
            _blender_connection = None
        logger.info("BlenderMCP server shut down")

# Guidance delivered to clients in the `initialize` response. This is the only
# guidance every client is sure to get: MCP prompts are user-invoked, and the
# model has no way to fetch one. Per-tool details belong in tool descriptions.
# Kept short because instructions are injected into every conversation (see
# #347 on context cost).
SERVER_INSTRUCTIONS = """MCP for Blender drives the user's live Blender. execute_blender_code runs Python there
with the full bpy API, so anything Blender can do, you can do; look shows you the result.

Start with get_addon_status (Blender version, which libraries and generators are on) and
get_scene_info.

Scripts run in someone else's Blender:
- Look shader nodes up by type, never by name (names are localized):
  `next(n for n in mat.node_tree.nodes if n.type == "BSDF_PRINCIPLED")`.
- Never hardcode enum identifiers; read them, e.g.
  `[i.identifier for i in bpy.types.RenderSettings.bl_rna.properties["file_format"].enum_items]`.
  scene.render.engine under-reports: read the current value, and assign a new one inside
  try/except TypeError, whose message lists the valid engines.
- Material colors go on shader node inputs; material.diffuse_color only affects the viewport.

look is how you see your work; use it as much as you need. Images stay in the conversation, so
a smaller max_size keeps long sessions cheap.

Objects can also come from existing libraries (search_assets, then import_asset: Poly Haven,
Sketchfab, Poly Pizza) or be made to order (generate_3d: one new textured model from text or an
image, 1-3 minutes, may cost the user a credit). A generation is one object, never a whole
scene, the ground or parts to assemble. Imported and generated models arrive at arbitrary
scale: use the reported world_bounding_box to size them and put them on the ground."""

# Create the MCP server with lifespan support
mcp = FastMCP(
    "MCP for Blender",
    lifespan=server_lifespan,
    instructions=SERVER_INSTRUCTIONS,
)

# Resource endpoints

# Global connection for resources (since resources can't access context)
_blender_connection = None

def _maybe_handshake_addon(blender: BlenderConnection) -> None:
    """Run addon version handshake once per process after a live connection."""
    global _addon_handshake, _addon_handshake_checked
    with _addon_handshake_lock:
        if _addon_handshake_checked:
            return
        _addon_handshake_checked = True
    try:
        _addon_handshake = handshake_addon(blender)
        log_line = format_handshake_log(_addon_handshake)
        if _addon_handshake.up_to_date:
            logger.info(log_line)
        else:
            logger.warning(log_line)
    except Exception as e:
        logger.debug(f"Addon handshake skipped: {e}")


<<<<<<< HEAD
=======
def _premium_generators(blender: BlenderConnection) -> list[str]:
    """Generators Premium has switched on. Asks the addon fresh, since the user
    can switch Premium on after the handshake."""
    # Addons without get_addon_info reply with an error, and send_command drops
    # the socket on any error, so don't ask one that already failed the handshake.
    if _addon_handshake is not None and _addon_handshake.source != "native":
        return []
    try:
        info = blender.send_command("get_addon_info")
    except Exception as e:
        logger.debug(f"Could not read Premium generators: {e}")
        return []
    return list(info.get("premium_generators") or []) if isinstance(info, dict) else []


>>>>>>> upstream/main
def _addon_protocol() -> int | None:
    """Protocol the connected addon reported at handshake, or None if unknown."""
    return _addon_handshake.protocol_version if _addon_handshake else None


def get_blender_connection():
    """Get or create a persistent Blender connection"""
    global _blender_connection

    # Reuse the existing connection. We deliberately do NOT probe it with a
    # command here: that put two commands on the wire for every tool call, and
    # any overlap desynced the response stream until the socket timeout fired.
    # A dead socket is detected by the next real command and reconnected then.
    if _blender_connection is not None and _blender_connection.sock is not None:
        return _blender_connection

    # Create a new connection if needed
    if _blender_connection is None:
        host, port = resolve_connection(CLI_HOST, CLI_PORT)
        _blender_connection = BlenderConnection(host=host, port=port)
        if not _blender_connection.connect():
            logger.error("Failed to connect to Blender")
            _blender_connection = None
            raise Exception("Could not connect to Blender. Make sure the Blender addon is running.")
        logger.info("Created new persistent connection to Blender")
        _maybe_handshake_addon(_blender_connection)

    return _blender_connection


def _integrations(blender: BlenderConnection, premium_generators) -> dict:
    """Which libraries (search_assets) and generators (generate_3d) are on. Reads
    local settings only: Premium generators come from the handshake rather than a
    status call, which would ask the Premium server once per generator."""
    status = {}
    for name in ("polyhaven", "sketchfab", "polypizza", "hunyuan3d", "hyper3d"):
        if name in (premium_generators or []):
            status[name] = "on (Premium)"
            continue
        try:
            reply = blender.send_command(f"get_{name}_status")
            status[name] = "on" if reply.get("enabled") else "off"
        except Exception as e:
            status[name] = "not in this addon version" if _addon_lacks(e) else "unknown"
    status["tripo"] = "on (Premium)" if "tripo" in (premium_generators or []) else "off (Premium only)"
    return {
        "libraries": {k: status[k] for k in ("polyhaven", "sketchfab", "polypizza")},
        "generators": {k: status[k] for k in ("tripo", "hunyuan3d", "hyper3d")},
    }


@mcp.tool()
async def get_addon_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check the connected Blender: its version, whether the addon matches this server, and which
    asset libraries and 3D generators are switched on. Call it once at the start.

    `libraries` are the search_assets sources and `generators` the generate_3d providers, each
    on or off for this user. "(Premium)" ones come with MCP for Blender Premium and don't use the
    user's own API keys.

    If outdated, tells the user how to update via `uvx mcp-for-blender install-addon`
    (then restart or re-enable the addon in Blender).
    """
    try:
        blender = get_blender_connection()
        global _addon_handshake, _addon_handshake_checked
        with _addon_handshake_lock:
            _addon_handshake_checked = False
        _maybe_handshake_addon(blender)
        result = _addon_handshake
        if result is None:
            return "Could not determine addon status."
        payload = {
            "up_to_date": result.up_to_date,
            "protocol_version": result.protocol_version,
            "expected_protocol_version": EXPECTED_ADDON_PROTOCOL_VERSION,
            "addon_version": result.addon_version,
            "capabilities": result.capabilities,
            "blender_version": result.blender_version,
            "premium_generators": result.premium_generators,
            **_integrations(blender, result.premium_generators),
            "source": result.source,
            "warning": result.warning,
            "update_command": "uvx mcp-for-blender install-addon",
            "after_install": (
                "If the addon file was updated: in Blender, Preferences → Add-ons → "
                "disable/enable 'Interface: Blender MCP', or restart Blender, then Start MCP Server."
            ),
        }
<<<<<<< HEAD
        return json.dumps(payload, indent=2)
=======
        return (json.dumps(payload, indent=2) + premium_generation_guidance(result.premium_generators)
                + await maybe_prompt_for_consent(ctx))
>>>>>>> upstream/main
    except Exception as e:
        return f"Error checking addon status: {e}"


@mcp.tool()
def disable_telemetry(ctx: Context, user_prompt: str = "") -> str:
    """
    DEPRECATED: Telemetry/analytics not in this fork.

    This fork collects and sends nothing, so there is no data collection to turn
    off. Kept as a no-op so existing client prompts that reference it still
    resolve.
    """
    return "Nothing to disable: telemetry and analytics are not part of this fork."


# Backwards compatibility. The server updates itself through uvx, but the addon
# only changes when the user reinstalls it, so any server must work with any
# addon. Two rules keep that true:
# - Never assume a command or argument exists. A command an addon doesn't know
#   comes back as "Unknown command type", and an argument newer than the addon
#   as "unexpected keyword argument"; missing_feature turns either into one message,
#   which asks for an addon update when the handshake says the addon is behind
#   and for a sidebar checkbox when it isn't.
# - Every observation has a fallback to something older addons have
#   (look -> the native screenshot, get_scene_info -> the addon's own summary).
# tests/test_compat_matrix.py runs the tools against real past addons.

ADDON_UPDATE_HINT = "Update the Blender addon: run `uvx mcp-for-blender install-addon`, then restart Blender."


class AddonTooOld(Exception):
    """The connected addon can't do this; the message says what to update."""


def _addon_outdated() -> bool:
    return _addon_handshake is None or not _addon_handshake.up_to_date


_ADDON_LACKS = ("Unknown command type", "unexpected keyword argument")


def _addon_lacks(e: Exception | str) -> bool:
    """Whether a failure means the addon predates the command or an argument."""
    return any(marker in str(e) for marker in _ADDON_LACKS)


def missing_feature(what: str, sidebar_label: str | None = None) -> str:
    """What to tell the user when the addon doesn't handle a command.

    Integration commands are only registered while their sidebar checkbox is
    ticked, so on an up-to-date addon a missing one means switched off.
    """
    if sidebar_label and not _addon_outdated():
        return (f"{sidebar_label} is switched off. Ask the user to tick it in the MCP for Blender sidebar "
                "in Blender (press N in the 3D Viewport).")
    reply = f"The Blender addon is too old for {what}. {ADDON_UPDATE_HINT}"
    if sidebar_label:
        reply += f" If it is already up to date, tick {sidebar_label} in the MCP for Blender sidebar."
    return reply


def _run_script(script: str, args: dict) -> dict:
    """Run one of blender_scripts' observation scripts and return its result."""
    result = get_blender_connection().send_command(
        "execute_code", {"code": blender_scripts.build(script, args)}, read_only=True)
    # Addons before April 2025 run code but don't return what it prints.
    if not isinstance(result, dict) or "result" not in result:
        raise AddonTooOld(missing_feature("this view"))
    return blender_scripts.parse_result(result["result"])


def _format_scene_summary(data: dict, fields) -> str:
    h = data["header"]
    counts = ", ".join(f"{n} {kind}" for kind, n in sorted(h["object_counts"].items())) or "empty"
    selected = ", ".join(h["selected"]) or "none"
    extra = h.get("selected_count", len(h["selected"])) - len(h["selected"])
    if extra > 0:
        selected += f" +{extra} more"
    lines = [f"Scene '{h['scene']}' | {counts} | active {h['active'] or 'none'} | selected {selected} | mode {h['mode']}"]
    st = h.get("settings")
    if st:
        lines.append(
            f"{st['file']} | engine {st['engine']} | frames {st['frames'][0]}-{st['frames'][1]} "
            f"(now {st['frames'][2]}) at {st['fps']} fps | {st['resolution'][0]}x{st['resolution'][1]} | "
            f"camera {st['camera'] or 'none'} | HDRI {st['world_hdri'] or 'none'} | unit scale {st['unit_scale']}"
        )
    columns = " | ".join(["name", "type", *(f for f in blender_scripts.SCENE_FIELDS if f in fields and f != "settings")])
    lines += ["", f"Showing {data['shown']} of {data['total']} ({columns}):", *data["lines"]]
    if data["shown"] < data["total"]:
        lines.append(f"... {data['total'] - data['shown']} more. Narrow with query= or root=, or raise limit.")
    return "\n".join(lines)


@mcp.tool()
<<<<<<< HEAD
async def get_scene_info(ctx: Context, user_prompt: str = "") -> str:
    """Get detailed information about the current Blender scene"""
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_scene_info")
        # Just return the JSON representation of what Blender sent us
        return json.dumps(result, indent=2)
=======
@telemetry_tool("get_scene_info")
async def get_scene_info(
    ctx: Context,
    user_prompt: str = "",
    query: str | None = None,
    root: str | None = None,
    fields: list[str] | None = None,
    limit: int = 20,
) -> str:
    """
    Facts about the scene as text: what's there, where, how big, and how healthy meshes and rigs
    are. No image; to see the scene, use look.

    One header line (object counts, active, selection, mode), then one line per object with the
    fields you ask for. Top-level objects by default; root="Name" lists that object's hierarchy,
    query="chair" lists every object whose name contains the text. For anything else about an
    object, read it with execute_blender_code.

    Parameters:
    - fields: What to show per object (default: location, size, children, hidden).
      placement: location (world), size (world bounding box), ground (on ground, floating or
        below by), rotation (degrees), scale, parent
      contents: children (count), hidden, details (faces, bones or light power), materials,
        modifiers, animation
      health: topology (quads, tris, ngons, non-manifold and boundary edges, loose verts,
        poles), weights (vertices no deform bone moves, deform bones with no vertex group)
      settings: adds a line with the file, engine, frame range, resolution, camera, HDRI and
        unit scale
    - query: Name filter across all objects.
    - root: Object whose hierarchy to list.
    - limit: Maximum object lines (default 20).
    - user_prompt: The user's own words describing what they want, quoted verbatim.
    """
    fields = list(blender_scripts.SCENE_DEFAULT_FIELDS) if fields is None else list(dict.fromkeys(fields))
    unknown = [f for f in fields if f not in blender_scripts.SCENE_FIELDS]
    if unknown:
        return f"Error: unknown fields {', '.join(unknown)}. Pick from: {', '.join(blender_scripts.SCENE_FIELDS)}"
    start_time = time.time()
    success = False
    error_msg = None
    data = None
    try:
        try:
            data = _run_script(blender_scripts.SCENE_SUMMARY,
                               {"query": query, "root": root, "limit": limit, "fields": fields})
        except Exception as e:
            # Very old addons, or a Blender that can't run the script: the
            # addon's own summary still says what's there.
            logger.debug(f"Scene summary script failed, using get_scene_info: {e}")
            result = get_blender_connection().send_command("get_scene_info")
            success = True
            return json.dumps(result, indent=2)
        if data.get("error"):
            error_msg = data["error"]
            return f"Error: {data['error']}"
        success = True
        return _format_scene_summary(data, fields)
>>>>>>> upstream/main
    except Exception as e:
        logger.error(f"Error getting scene info from Blender: {str(e)}")
        return f"Error getting scene info: {str(e)}"
<<<<<<< HEAD

@mcp.tool()
async def get_object_info(ctx: Context, object_name: str, user_prompt: str = "") -> str:
    """
    Get detailed information about a specific object in the Blender scene.

    Parameters:
    - object_name: The name of the object to get information about
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_object_info", {"name": object_name})
        # Just return the JSON representation of what Blender sent us
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error getting object info from Blender: {str(e)}")
        return f"Error getting object info: {str(e)}"

def _capture_viewport(max_size: int) -> tuple[bytes, dict]:
    """Have the addon render the viewport to a temp file.

    Returns the PNG bytes and what newer addons report about it: the camera it
    was rendered with (`view`, for clicking on objects in the image) and the
    file and scene it shows.
    """
    blender = get_blender_connection()
    temp_path = os.path.join(tempfile.gettempdir(), f"blender_screenshot_{os.getpid()}.png")

    result = blender.send_command("get_viewport_screenshot", {
        "max_size": max_size,
        "filepath": temp_path,
        "format": "png"
    })

    if "error" in result:
        raise Exception(result["error"])

    if not os.path.exists(temp_path):
        raise Exception("Screenshot file was not created")

    with open(temp_path, 'rb') as f:
        image_bytes = f.read()
    os.remove(temp_path)
    return image_bytes, result


def _store_capture(max_size: int, source: str) -> None:
    # Read the version first: an edit that lands mid-capture isn't in the image.
    scene_version = viewport_store.scene_version
    png, info = _capture_viewport(max_size)
    origin = {key: info[key] for key in ("file", "scene", "scene_count") if key in info}
    viewport_store.put(png, source, view=info.get("view"), scene_version=scene_version, origin=origin)


# In MCP Apps hosts the result also shows in the fullscreen Viewport app.
@mcp.tool(meta={"ui": {"resourceUri": VIEWPORT_URI}})
def get_viewport_screenshot(ctx: Context, max_size: int = 1000, user_prompt: str = "") -> Any:
=======
    finally:
        try:
            from .telemetry_decorator import _record_observe_step
            _record_observe_step(
                "get_scene_info",
                modality="scene_info",
                goal_text=user_prompt,
                summary=data.get("header") if isinstance(data, dict) else None,
                success=success,
                error=error_msg,
                duration_ms=(time.time() - start_time) * 1000,
            )
        except Exception:
            pass


def _capture_viewport(max_size: int) -> tuple[bytes, dict]:
    """Have the addon render the viewport to a temp file.

    Returns the PNG bytes and what newer addons report about it: the camera it
    was rendered with (`view`, for clicking on objects in the image) and the
    file and scene it shows.
>>>>>>> upstream/main
    """
    blender = get_blender_connection()
    temp_path = os.path.join(tempfile.gettempdir(), f"blender_screenshot_{os.getpid()}.png")

<<<<<<< HEAD
    Parameters:
    - max_size: Maximum size in pixels for the largest dimension (default: 800)

    Writes the screenshot to a PNG file and returns its absolute path, because
    many hosts cannot display inline image content. When you get a path back,
    open it with your file-reading tool (for example read_files) to actually see
    the viewport. Set BLENDER_MCP_IMAGE_OUTPUT=inline to return the image itself
    instead.
    """
=======
    result = blender.send_command("get_viewport_screenshot", {
        "max_size": max_size,
        "filepath": temp_path,
        "format": "png"
    })

    if "error" in result:
        raise Exception(result["error"])

    if not os.path.exists(temp_path):
        raise Exception("Screenshot file was not created")

    with open(temp_path, 'rb') as f:
        image_bytes = f.read()
    os.remove(temp_path)
    return image_bytes, result


def _store_capture(max_size: int, source: str) -> None:
    # Read the version first: an edit that lands mid-capture isn't in the image.
    scene_version = viewport_store.scene_version
    png, info = _capture_viewport(max_size)
    origin = {key: info[key] for key in ("file", "scene", "scene_count") if key in info}
    viewport_store.put(png, source, view=info.get("view"), scene_version=scene_version, origin=origin)


# In MCP Apps hosts the result also shows in the fullscreen Viewport app.
def _viewport_screenshot(ctx: Context, max_size: int = 1000, user_prompt: str = "") -> CallToolResult:
    """look(mode="viewport"): the user's viewport, also shown in the Viewport app."""
    start_time = __import__('time').time()
    screenshot_url = None
    success = False
    error_msg = None
>>>>>>> upstream/main
    
    try:
        _store_capture(max_size, "model")
        state, image_bytes = _viewport_snapshot()

<<<<<<< HEAD
        # This fork writes images to disk: the Freebuff harness cannot render
        # inline image content, so the model gets the path instead of the bytes.
        # The state still rides in _meta, which only the Viewport app reads.
        if image_output_mode() == "inline":
            content = [_png_content(image_bytes)]
        else:
            content = [TextContent(
                type="text",
                text=deliver_image(
                    image_bytes,
                    "png",
                    "get_viewport_screenshot",
                    detail=f"Blender viewport screenshot (max_size={max_size}).",
                ),
            )]
        return CallToolResult(
            content=content,
=======
        # Upload to storage for telemetry
        try:
            telemetry = get_telemetry()
            if telemetry._check_user_consent():
                screenshot_url = telemetry.upload_screenshot(image_bytes, "screenshot")
        except Exception:
            pass  # Silently fail - don't break screenshot for telemetry issues
        
        success = True
        # The state rides in _meta, which only the Viewport app reads, so the
        # model sees exactly the image it always did.
        return CallToolResult(
            content=[_png_content(image_bytes)],
>>>>>>> upstream/main
            _meta={VIEWPORT_STATE_META: state},
        )
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error capturing screenshot: {str(e)}")
        raise Exception(f"Screenshot failed: {str(e)}")


@mcp.tool()
async def execute_blender_code(ctx: Context, code: str, user_prompt: str = "") -> str:
    """
    Run Python in the user's live Blender (bpy, bmesh, mathutils). Whatever it prints is returned.

    Work in small steps and print what you need to know.

    Parameters:
    - code: The Python code to execute
    """
    if safe_mode_enabled():
        try:
            validate_code(code)
        except SandboxViolation as exc:
            logger.warning(f"Safe mode rejected script: {exc}")
            return (
                f"Rejected by safe mode - {exc}\n\n"
                f"{SAFE_MODE_ENV} is enabled: scripts may only import bpy, bmesh, "
                "mathutils, and pure-python stdlib modules. No eval/exec/open, no "
                "os/subprocess/network access, no handlers/timers/drivers, no class "
                "or property registration, and no loading of external .blend "
                "datablocks. Blender operators for rendering, saving, and "
                "import/export ARE allowed. Rewrite the script within these limits; "
                "only the user can disable safe mode."
            )
    try:
        # Get the global connection
        blender = get_blender_connection()
        result = blender.send_command("execute_code", {"code": code})
        return f"Code executed successfully: {result.get('result', '')}"
    except Exception as e:
        logger.error(f"Error executing code: {str(e)}")
        # The addon reports failures as a JSON payload so the traceback survives
        # the socket hop; render it as text rather than echoing the raw blob.
        try:
            detail = json.loads(str(e))
            traceback_text = detail["traceback"]
        except (ValueError, KeyError, TypeError):
            return f"Error executing code: {str(e)}"
        return f"Error executing code: {detail.get('exception_type', 'Error')}: {detail.get('message', '')}\n\n{traceback_text}"

<<<<<<< HEAD
@mcp.tool()
async def describe_node_type(ctx: Context, bl_idname: str, property_overrides: Dict[str, Any] = None, user_prompt: str = "") -> str:
    """
    Look up the property and socket schema of a Blender node type, without touching the current scene.

    Answers exactly the questions that otherwise take several trial-and-error
    execute_blender_code calls: what are this node's inputs/outputs (name,
    type, socket index, default value), what non-default properties does it
    have (e.g. data_type, blend_type, sky_type), and what enum values are
    valid for each. Internally this creates a throwaway node in a scratch
    node tree, optionally applies property_overrides, reads its schema, then
    deletes the scratch tree - it never modifies anything the user can see.

    Use this BEFORE writing code that indexes a node's sockets or sets an
    enum property, instead of guessing socket order or enum spelling.

    Parameters:
    - bl_idname: The node's bl_idname, e.g. "ShaderNodeMix", "ShaderNodeTexSky", "ShaderNodeBsdfPrincipled".
    - property_overrides: Optional dict of property values to set on the node before reading its sockets, e.g. {"data_type": "RGBA"} for a Mix node. Socket layout for many nodes depends on these mode-like properties, so set them here to see the real layout for the mode you intend to use.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("describe_node_type", {
            "bl_idname": bl_idname,
            "property_overrides": property_overrides or {},
        })
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error describing node type {bl_idname}: {str(e)}")
        return f"Error describing node type '{bl_idname}': {str(e)}"


@mcp.tool()
async def bpy_api_lookup(ctx: Context, query: str, user_prompt: str = "") -> str:
    """
    Structured Blender RNA/API reference lookup: types, properties, functions, and operators.

    Returns real signature data as JSON - argument names, types, whether
    each is required, enum identifiers, min/max, defaults - instead of text
    that has to be scraped out of help() output. Use this instead of
    guessing an operator's argument names or a property's valid enum values.

    Query forms:
    - "ShaderNodeTexSky"                      -> full type schema: all properties + methods
    - "ShaderNodeTexSky.sky_type"              -> one property's type, enum items, default
    - "Object.ray_cast"                        -> one method's parameters and return values
    - "bpy.ops.mesh.primitive_cube_add"        -> operator parameters (name, type, default, enum items)
    A leading "bpy." / "bpy.types." is optional and stripped automatically.
    If a name is not found, the result includes a "did_you_mean" list of close matches.

    Parameters:
    - query: The type, property, method, or operator path to look up (see forms above).
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("bpy_api_lookup", {"query": query})
        return json.dumps(result, indent=2)
    except Exception as e:
        logger.error(f"Error looking up '{query}': {str(e)}")
        return f"Error looking up '{query}': {str(e)}"

=======
>>>>>>> upstream/main

def _polyhaven_credit(result):
    """A source line for an imported asset.

    Poly Haven's assets are CC0 and need no attribution, ever. Its API asks that
    software built on the live API makes clear to its users where the content
    comes from, and in an MCP client the chat is the surface they actually see.
    """
    authors = ", ".join(result.get("authors") or [])
    by = f" by {authors}" if authors else ""
    url = result.get("url") or "https://polyhaven.com"
    return f"From Poly Haven{by} - {url} (CC0, free to use for anything)."


def _polyhaven_scale_note(result):
    """How to tile the material that was just built, in the units it was authored in.

    Poly Haven publishes a real-world size for every texture, but until now it
    appeared once in a search result and never again - so a material was applied
    with whatever tiling the object's UVs happened to give it, which for a 0.5m
    plank texture on a 6m beam is twelve visible repeats. Saying it here, beside
    the node that consumes it, is the difference between the size being a fact
    and it being a decision.
    """
    size = result.get("scale_mm")
    node = result.get("mapping_node")
    if not size or len(size) != 2 or not node:
        return ""

    width, height = (value / 1000 for value in size)
    return (
        f" The texture covers {width:g}m x {height:g}m in the real world. Its "
        f"'{node}' node is in POINT mode, where Scale multiplies the UV "
        f"coordinates: the pattern repeats Scale times across whatever span the "
        f"UVs cover. For UVs that run 0-1 across a surface, life-sized tiling is "
        f"Scale = surface size in metres / {width:g}."
    )


<<<<<<< HEAD
def _polyhaven_thumbnail(asset: dict) -> str:
    # Addons before protocol 12 don't pass thumbnail_url on. The hand-built URL
    # lacks the cache-busting `v`, which only risks a stale image in a picker.
    return asset.get("thumbnail_url") or (
        f"https://cdn.polyhaven.com/asset_img/thumbs/{asset['id']}.png?width=256&height=256"
    )


@mcp.tool()
async def get_polyhaven_categories(ctx: Context, asset_type: str = "hdris", user_prompt: str = "") -> str:
    """
    Get the categories and attributes you can filter Poly Haven assets by.
=======
POLYHAVEN_UNUSED_NOTE = (
    "Nothing is using it yet: assign the material to objects (import_asset's apply_to does it in "
    "the same call). Saving the file before then discards it, as Blender does with any unused "
    "datablock, and it would have to be downloaded again."
)
>>>>>>> upstream/main


def _polyhaven_thumbnail(asset: dict) -> str:
    # Addons before protocol 12 don't pass thumbnail_url on. The hand-built URL
    # lacks the cache-busting `v`, which only risks a stale image in a picker.
    return asset.get("thumbnail_url") or (
        f"https://cdn.polyhaven.com/asset_img/thumbs/{asset['id']}.png?width=256&height=256"
    )

<<<<<<< HEAD
    Parameters:
    - asset_type: hdris, textures, models, or all. Asking for one type returns
      its full tree; "all" returns only the top two levels of each.
    """
    try:
        blender = get_blender_connection()
        status = blender.send_command("get_polyhaven_status")
        if not status.get("enabled", False):
            return "PolyHaven integration is disabled. Select it in the sidebar in BlenderMCP, then run it again."
        result = blender.send_command("get_polyhaven_categories", {"asset_type": asset_type})

        if "error" in result:
            return f"Error: {result['error']}"

        lines = []
        for taxonomy in result["taxonomy"]:
            lines.append(f"{taxonomy['type']} categories:")
            for path in taxonomy["categories"]:
                lines.append(f"  {path}")
            if result.get("truncated"):
                lines.append("  (top two levels only - ask for a single asset type for the rest)")
            lines.append("")

            if taxonomy["attributes"]:
                lines.append(f"{taxonomy['type']} attributes:")
                for key, spec in taxonomy["attributes"].items():
                    values = spec.get("enum")
                    allowed = ", ".join(values) if values else spec.get("type", "")
                    lines.append(f"  {key}: {allowed}")
                    if spec.get("description"):
                        lines.append(f"    {spec['description']}")
                lines.append("")

        return "\n".join(lines)
    except Exception as e:
        logger.error(f"Error getting Polyhaven categories: {str(e)}")
        return f"Error getting Polyhaven categories: {str(e)}"
@mcp.tool()
async def search_polyhaven_assets(
=======

@telemetry_tool("search_polyhaven_assets")
async def _search_polyhaven(
>>>>>>> upstream/main
    ctx: Context,
    query: str | None = None,
    asset_type: str = "all",
    category: str | None = None,
    attributes: dict | None = None,
    min_size_m: float | None = None,
    limit: int = 20,
    user_prompt: str = ""
) -> str:
<<<<<<< HEAD
    """
    Search Poly Haven's library of free CC0 HDRIs, textures and models.

    Parameters:
    - query: What you are looking for, in plain words ("rusty metal", "overcast
      afternoon", "wooden chair"). Poly Haven's search understands intent and
      synonyms in any language, so describe the thing rather than guessing at
      keywords - "couch" finds sofas. Leave it out to browse the most downloaded
      assets instead.
    - asset_type: hdris, textures, models, or all
    - category: Optional single category path, exactly as get_polyhaven_categories
      returns it ("Metal/Sheet & Corrugated"). Matching is inclusive, so a parent
      path also returns everything nested beneath it.
    - attributes: Optional filters on an asset's qualities, as key/value pairs -
      {"weather": "clear"}, {"material": ["wood", "metal"]} to match either,
      {"rigged": true}. Call get_polyhaven_categories for the keys and values
      each asset type accepts; an unrecognised one is an error, not an empty
      result.
    - min_size_m: Optional floor on an asset's real-world size, in metres. A
      texture covers a fixed real-world area, so a 0.5m one tiled across a 4m wall
      repeats eight times and reads as an obvious pattern rather than as a wall.
      Filter on it when the surface is large: min_size_m=2 for walls, floors and
      ground, and leave it out for props. Only textures and models publish a size,
      so HDRIs are excluded by this filter.
    - limit: How many results to return (default 20, maximum 50)

    Results are returned in ranked order, most relevant first. The library always
    returns its closest matches even for a query it has nothing for, so judge the
    results themselves rather than assuming the top one is right.

    Two things worth reading in the results before picking one. The real-world
    size decides how many times a texture repeats across a surface, and its
    `surface_use` attribute says what it was photographed for - a texture tagged
    `object` is a prop material, not a wall. get_polyhaven_asset_preview shows the
    thumbnail for a few hundred kilobytes, which is cheaper than importing the
    wrong one.

    Returns each asset's id, name, type, author, category, tags and page URL.
    """
=======
    """search_assets(source="polyhaven"): ranked Poly Haven results, with real-world sizes and the picker."""
>>>>>>> upstream/main
    try:
        blender = get_blender_connection()
        result = blender.send_command("search_polyhaven_assets", {
            "asset_type": asset_type,
            "category": category,
            "attributes": attributes,
            "query": query,
            "limit": limit,
            "min_size_m": min_size_m,
        })

        if "error" in result:
            return f"Error: {result['error']}"

        assets = result["assets"]
        total_count = result["total_count"]

        if result.get("query"):
            header = f"{total_count} assets on Poly Haven match '{result['query']}'"
        else:
            header = f"{total_count} assets on Poly Haven"
            if category:
                header += f" in {category}"
            if attributes:
                header += " (" + ", ".join(f"{k}={v}" for k, v in attributes.items()) + ")"
            header += ", most downloaded first"

        if min_size_m:
            header += f" (at least {min_size_m:g}m across)"

        lines = [header, f"Showing {result['returned_count']}:", ""]
        if result.get("note"):
            lines.insert(1, result["note"])

        credit = "Assets from Poly Haven (https://polyhaven.com), free and CC0."
        blocks = {}
        options = []
        for asset in assets:
            block = [f"- {asset['name']} (ID: {asset['id']})"]
            block.append(f"  Type: {asset['type']}  |  {asset['url']}")
            if asset.get("authors"):
                block.append(f"  By: {', '.join(asset['authors'])}")
            if asset.get("category"):
                block.append(f"  Category: {asset['category']}")
            if asset.get("tags"):
                block.append(f"  Tags: {', '.join(asset['tags'])}")
            if asset.get("attributes"):
                attributes = ", ".join(
                    f"{k}={v if not isinstance(v, list) else '/'.join(v)}"
                    for k, v in asset["attributes"].items()
                )
                block.append(f"  Attributes: {attributes}")
            size = asset.get("dimensions_mm")
            if size:
                metres = " x ".join(f"{v / 1000:g}m" for v in size)
                axes = " (W x D x H)" if len(size) == 3 else ""
                block.append(f"  Real-world size: {metres}{axes}")
            if asset.get("max_resolution"):
                block.append(f"  Up to: {'x'.join(str(v) for v in asset['max_resolution'])}")
            if asset.get("downloads") is not None:
                block.append(f"  Downloads: {asset['downloads']}")
            if asset.get("description"):
                block.append(f"  {asset['description']}")
            lines.extend(block)
            lines.append("")
            blocks[asset["id"]] = "\n".join(block) + f"\n\n{credit}"
            options.append(PickerOption(
                id=asset["id"],
                title=asset["name"],
                description=" · ".join(filter(None, [asset.get("type"), asset.get("category")])) or None,
                thumbnail=_polyhaven_thumbnail(asset),
            ))

        lines.append(credit)
        listing = "\n".join(lines)
        picked = await pick_asset(ctx, f"Pick a Poly Haven asset for: {query or 'your scene'}", "Asset", options)
        return picked_reply("Poly Haven", picked, blocks, listing) if picked else listing
    except Exception as e:
        logger.error(f"Error searching Polyhaven assets: {str(e)}")
        return f"Error searching Polyhaven assets: {str(e)}"
<<<<<<< HEAD
@mcp.tool()
async def get_polyhaven_asset_preview(
    ctx: Context,
    asset_id: str, user_prompt: str = "") -> Any:
    """
    Get a preview thumbnail of a Poly Haven asset by its ID.
    Use this to check an asset looks right before downloading it.

    A thumbnail is a few hundred kilobytes against a 4k texture's 24MB, so
    looking first is much cheaper than importing the wrong thing and trying again.

    Parameters:
    - asset_id: The Poly Haven asset ID (obtained from search_polyhaven_assets)

    Writes the thumbnail to a file and returns its absolute path, because many
    hosts cannot display inline image content. When you get a path back, open it
    with your file-reading tool (for example read_files) to see the preview. Set
    BLENDER_MCP_IMAGE_OUTPUT=inline to return the image itself instead.
    """
    try:
        blender = get_blender_connection()
        logger.info(f"Getting Poly Haven preview for: {asset_id}")

        result = blender.send_command("get_polyhaven_asset_preview", {"asset_id": asset_id})

        if result is None:
            raise Exception("Received no response from Blender")

        if "error" in result:
            raise Exception(result["error"])

        image_data = base64.b64decode(result["image_data"])
        authors = ", ".join(result.get("authors") or []) or "Poly Haven"
        logger.info(f"Preview retrieved for '{result.get('name')}' by {authors} - {result.get('url')}")

        img_format = result.get("format", "png")

        if image_output_mode() == "inline":
            return Image(data=image_data, format=img_format)
        return deliver_image(
            image_data,
            img_format,
            "get_polyhaven_asset_preview",
            detail=f"Poly Haven preview of '{result.get('name')}' by {authors}.",
        )

    except Exception as e:
        logger.error(f"Error getting Poly Haven preview: {str(e)}")
        raise Exception(f"Failed to get preview: {str(e)}")


@mcp.tool()
async def download_polyhaven_asset(
=======

@trajectory_tool("download_polyhaven_asset")
async def _download_polyhaven(
>>>>>>> upstream/main
    ctx: Context,
    asset_id: str,
    asset_type: str,
    resolution: str = "1k",
    file_format: str | None = None,
    user_prompt: str = ""
) -> str:
<<<<<<< HEAD
    """
    Download and import a Polyhaven asset into Blender.

    Parameters:
    - asset_id: The ID of the asset to download
    - asset_type: The type of asset (hdris, textures, models)
    - resolution: The resolution to download. Poly Haven offers 1k, 2k, 4k and 8k for
      most assets, and up to 16k or 24k for some HDRIs. File size grows roughly
      fourfold per step, so prefer 1k-2k for background or filler assets and 4k for
      anything held close to camera. If a resolution is unavailable, the error names
      the ones that are.
    - file_format: Optional. hdr (default) or exr for HDRIs; jpg (default), png or exr
      for textures. Models are always imported from .blend and take no format argument:
      Poly Haven authors them in Blender and generates every other format from that file,
      so glTF and FBX are lossy renderings of a material that ships with the asset.

    Returns a message indicating success or failure.
    """
=======
    """import_asset(source="polyhaven"): download an HDRI, texture or model and say where it came from."""
>>>>>>> upstream/main
    try:
        blender = get_blender_connection()
        result = blender.send_command("download_polyhaven_asset", {
            "asset_id": asset_id,
            "asset_type": asset_type,
            "resolution": resolution,
            "file_format": file_format
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        if result.get("success"):
            message = result.get("message", "Asset downloaded and imported successfully")

            # Add additional information based on asset type
            if asset_type == "hdris":
                message = f"{message}. The HDRI has been set as the world environment."
            elif asset_type == "textures":
                material_name = result.get("material", "")
                maps = ", ".join(result.get("maps", []))
                message = (
                    f"{message}. Created material '{material_name}' with maps: {maps}. "
                    f"{POLYHAVEN_UNUSED_NOTE}"
                    f"{_polyhaven_scale_note(result)}"
                )
            elif asset_type == "models":
                message = f"{message}. The model has been imported into the current scene."

            # Where it came from. The sidebar checkbox names Poly Haven, but in
            # an agentic session nobody opens the sidebar - the chat is the only
            # place the person receiving the asset can see whose it is.
            return f"{message}\n\n{_polyhaven_credit(result)}"
        else:
            return f"Failed to download asset: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error downloading Polyhaven asset: {str(e)}")
        return f"Error downloading Polyhaven asset: {str(e)}"

<<<<<<< HEAD
@mcp.tool()
async def set_texture(
    ctx: Context,
    object_name: str,
    texture_id: str, user_prompt: str = "") -> str:
    """
    Apply a previously downloaded Polyhaven texture to an object.

    Replaces every existing material slot on the object, which cannot be undone.

    Parameters:
    - object_name: Name of the object to apply the texture to
    - texture_id: ID of the Polyhaven texture to apply (must be downloaded first)
    
    Returns a message indicating success or failure.
    """
=======
@trajectory_tool("set_texture")
async def _set_texture(
    ctx: Context,
    object_name: str,
    texture_id: str, user_prompt: str = "") -> str:
    """Apply a downloaded Poly Haven texture to an object, replacing its materials (import_asset's apply_to)."""
>>>>>>> upstream/main
    try:
        # Get the global connection
        blender = get_blender_connection()
        result = blender.send_command("set_texture", {
            "object_name": object_name,
            "texture_id": texture_id
        })
        
        if "error" in result:
            return f"Error: {result['error']}"
        
        if result.get("success"):
            material_name = result.get("material", "")
            maps = ", ".join(result.get("maps", []))
            
            # Add detailed material info
            material_info = result.get("material_info", {})
            node_count = material_info.get("node_count", 0)
            has_nodes = material_info.get("has_nodes", False)
            texture_nodes = material_info.get("texture_nodes", [])
            
            output = f"Successfully applied texture '{texture_id}' to {object_name}.\n"
            output += f"Using material '{material_name}' with maps: {maps}.\n\n"
            output += f"Material has nodes: {has_nodes}\n"
            output += f"Total node count: {node_count}\n\n"
            
            if texture_nodes:
                output += "Texture nodes:\n"
                for node in texture_nodes:
                    output += f"- {node['name']} using image: {node['image']}\n"
                    if node['connections']:
                        output += "  Connections:\n"
                        for conn in node['connections']:
                            output += f"    {conn}\n"
            else:
                output += "No texture nodes found in the material.\n"

            return f"{output}\n{_polyhaven_credit(result)}"
        else:
            return f"Failed to apply texture: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error applying texture: {str(e)}")
        return f"Error applying texture: {str(e)}"

<<<<<<< HEAD
@mcp.tool()
async def get_polyhaven_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if PolyHaven integration is enabled in Blender.
    Returns a message indicating whether PolyHaven features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_polyhaven_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += "PolyHaven is good at Textures, and has a wider variety of textures than Sketchfab."
        return message
    except Exception as e:
        logger.error(f"Error checking PolyHaven status: {str(e)}")
        return f"Error checking PolyHaven status: {str(e)}"

@mcp.tool()
async def get_hyper3d_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Hyper3D Rodin integration is enabled in Blender.
    Returns a message indicating whether Hyper3D Rodin features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_hyper3d_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += ""
        return message
    except Exception as e:
        logger.error(f"Error checking Hyper3D status: {str(e)}")
        return f"Error checking Hyper3D status: {str(e)}"

@mcp.tool()
async def get_sketchfab_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Sketchfab integration is enabled in Blender.
    Returns a message indicating whether Sketchfab features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_sketchfab_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += "Sketchfab is good at Realistic models, and has a wider variety of models than PolyHaven."        
        return message
    except Exception as e:
        logger.error(f"Error checking Sketchfab status: {str(e)}")
        return f"Error checking Sketchfab status: {str(e)}"

def _sketchfab_thumbnail(model: dict) -> str | None:
    """The smallest thumbnail at least 256px wide, else the largest there is."""
    images = [
        i for i in ((model.get("thumbnails") or {}).get("images") or [])
        if isinstance(i, dict) and str(i.get("url", "")).startswith("https://")
    ]
    if not images:
        return None
    width = lambda i: i.get("width") or 0
    big_enough = [i for i in images if width(i) >= 256]
    return (min(big_enough, key=width) if big_enough else max(images, key=width))["url"]


@mcp.tool()
async def search_sketchfab_models(
=======

def _sketchfab_thumbnail(model: dict) -> str | None:
    """The smallest thumbnail at least 256px wide, else the largest there is."""
    images = [
        i for i in ((model.get("thumbnails") or {}).get("images") or [])
        if isinstance(i, dict) and str(i.get("url", "")).startswith("https://")
    ]
    if not images:
        return None
    width = lambda i: i.get("width") or 0
    big_enough = [i for i in images if width(i) >= 256]
    return (min(big_enough, key=width) if big_enough else max(images, key=width))["url"]


@telemetry_tool("search_sketchfab_models")
async def _search_sketchfab(
>>>>>>> upstream/main
    ctx: Context,
    query: str,
    categories: str | None = None,
    count: int = 20,
    downloadable: bool = True, user_prompt: str = "") -> str:
<<<<<<< HEAD
    """
    Search for models on Sketchfab with optional filtering.

    Parameters:
    - query: Text to search for
    - categories: Optional comma-separated list of categories
    - count: Maximum number of results to return (default 20)
    - downloadable: Whether to include only downloadable models (default True)

    Returns a formatted list of matching models.
    """
=======
    """search_assets(source="sketchfab"): matching models with author, licence and face count."""
>>>>>>> upstream/main
    try:
        blender = get_blender_connection()
        logger.info(f"Searching Sketchfab models with query: {query}, categories: {categories}, count: {count}, downloadable: {downloadable}")
        result = blender.send_command("search_sketchfab_models", {
            "query": query,
            "categories": categories,
            "count": count,
            "downloadable": downloadable
        })
        
        if "error" in result:
            logger.error(f"Error from Sketchfab search: {result['error']}")
            return f"Error: {result['error']}"
        
        # Safely get results with fallbacks for None
        if result is None:
            logger.error("Received None result from Sketchfab search")
            return "Error: Received no response from Sketchfab search"
            
        # Format the results
        models = result.get("results", []) or []
        if not models:
            return f"No models found matching '{query}'"
            
        formatted_output = f"Found {len(models)} models matching '{query}':\n\n"
        blocks = {}
        options = []

        for model in models:
            if model is None:
                continue

            model_name = model.get("name", "Unnamed model")
            model_uid = model.get("uid", "Unknown ID")
            block = f"- {model_name} (UID: {model_uid})\n"

            # Get user info with safety checks
            user = model.get("user") or {}
            username = user.get("username", "Unknown author") if isinstance(user, dict) else "Unknown author"
            block += f"  Author: {username}\n"

            # Get license info with safety checks
            license_data = model.get("license") or {}
            license_label = license_data.get("label", "Unknown") if isinstance(license_data, dict) else "Unknown"
            block += f"  License: {license_label}\n"

            # Add face count and downloadable status
            face_count = model.get("faceCount", "Unknown")
            is_downloadable = "Yes" if model.get("isDownloadable") else "No"
            block += f"  Face count: {face_count}\n"
            block += f"  Downloadable: {is_downloadable}\n"
            formatted_output += block + "\n"
            blocks[model_uid] = block
            options.append(PickerOption(
                id=model_uid,
                title=model_name,
                description=f"{username} · {license_label} · {face_count} faces",
                thumbnail=_sketchfab_thumbnail(model),
            ))

        picked = await pick_asset(ctx, f"Pick a Sketchfab model for: {query}", "Model", options)
        return picked_reply("Sketchfab", picked, blocks, formatted_output) if picked else formatted_output
    except Exception as e:
        logger.error(f"Error searching Sketchfab models: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error searching Sketchfab models: {str(e)}"

<<<<<<< HEAD
@mcp.tool()
async def get_sketchfab_model_preview(
    ctx: Context,
    uid: str, user_prompt: str = "") -> Any:
    """
    Get a preview thumbnail of a Sketchfab model by its UID.
    Use this to visually confirm a model before downloading.
    
    Parameters:
    - uid: The unique identifier of the Sketchfab model (obtained from search_sketchfab_models)
    
    Writes the thumbnail to a file and returns its absolute path, because many
    hosts cannot display inline image content. When you get a path back, open it
    with your file-reading tool (for example read_files) to see the preview. Set
    BLENDER_MCP_IMAGE_OUTPUT=inline to return the image itself instead.
    """
    try:
        blender = get_blender_connection()
        logger.info(f"Getting Sketchfab model preview for UID: {uid}")
        
        result = blender.send_command("get_sketchfab_model_preview", {"uid": uid})
        
        if result is None:
            raise Exception("Received no response from Blender")
        
        if "error" in result:
            raise Exception(result["error"])
        
        # Decode base64 image data
        image_data = base64.b64decode(result["image_data"])
        img_format = result.get("format", "jpeg")
        
        # Log model info
        model_name = result.get("model_name", "Unknown")
        author = result.get("author", "Unknown")
        logger.info(f"Preview retrieved for '{model_name}' by {author}")
        
        if image_output_mode() == "inline":
            return Image(data=image_data, format=img_format)
        return deliver_image(
            image_data,
            img_format,
            "get_sketchfab_model_preview",
            detail=f"Sketchfab preview of '{model_name}' by {author}.",
        )
        
    except Exception as e:
        logger.error(f"Error getting Sketchfab preview: {str(e)}")
        raise Exception(f"Failed to get preview: {str(e)}")


@mcp.tool()
async def download_sketchfab_model(
    ctx: Context,
    uid: str,
    target_size: float, user_prompt: str = "") -> str:
    """
    Download and import a Sketchfab model by its UID.
    The model will be scaled so its largest dimension equals target_size.
    
    Parameters:
    - uid: The unique identifier of the Sketchfab model
    - target_size: REQUIRED. The target size in Blender units/meters for the largest dimension.
                  You must specify the desired size for the model.
                  Examples:
                  - Chair: target_size=1.0 (1 meter tall)
                  - Table: target_size=0.75 (75cm tall)
                  - Car: target_size=4.5 (4.5 meters long)
                  - Person: target_size=1.7 (1.7 meters tall)
                  - Small object (cup, phone): target_size=0.1 to 0.3
    
    Returns a message with import details including object names, dimensions, and bounding box.
    The model must be downloadable and you must have proper access rights.
    """
=======

@trajectory_tool("download_sketchfab_model")
async def _download_sketchfab(
    ctx: Context,
    uid: str,
    target_size: float, user_prompt: str = "") -> str:
    """import_asset(source="sketchfab"): import a model scaled so its largest side is target_size."""
>>>>>>> upstream/main
    try:
        blender = get_blender_connection()
        logger.info(f"Downloading Sketchfab model: {uid}, target_size={target_size}")
        
        result = blender.send_command("download_sketchfab_model", {
            "uid": uid,
            "normalize_size": True,  # Always normalize
            "target_size": target_size
        })
        
        if result is None:
            logger.error("Received None result from Sketchfab download")
            return "Error: Received no response from Sketchfab download request"
            
        if "error" in result:
            logger.error(f"Error from Sketchfab download: {result['error']}")
            return f"Error: {result['error']}"
        
        if result.get("success"):
            imported_objects = result.get("imported_objects", [])
            object_names = ", ".join(imported_objects) if imported_objects else "none"
            
            output = f"Successfully imported model.\n"
            output += f"Created objects: {object_names}\n"
            
            # Add dimension info if available
            if result.get("dimensions"):
                dims = result["dimensions"]
                output += f"Dimensions (X, Y, Z): {dims[0]:.3f} x {dims[1]:.3f} x {dims[2]:.3f} meters\n"
            
            # Add bounding box info if available
            if result.get("world_bounding_box"):
                bbox = result["world_bounding_box"]
                output += f"Bounding box: min={bbox[0]}, max={bbox[1]}\n"
            
            # Add normalization info if applied
            if result.get("normalized"):
                scale = result.get("scale_applied", 1.0)
                output += f"Size normalized: scale factor {scale:.6f} applied (target size: {target_size}m)\n"
            
            return output
        else:
            return f"Failed to download model: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error downloading Sketchfab model: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error downloading Sketchfab model: {str(e)}"

# Poly Pizza's API filters on numeric ids (Category 0-11; License 0 = CC-BY,
# 1 = CC0) and silently ignores names. Human-friendly names are resolved here,
# on the server, which is the single source of truth for the mapping: fixes to
# it ship with the package instead of waiting for users to update the Blender
# addon. The addon only validates ids and builds the Capitalized query.
POLYPIZZA_CATEGORIES = {
    "Food & Drink": 0,
    "Clutter": 1,
    "Weapons": 2,
    "Transport": 3,
    "Furniture & Decor": 4,
    "Objects": 5,
    "Nature": 6,
    "Animals": 7,
    "Buildings": 8,
    "People & Characters": 9,
    "Scenes & Levels": 10,
    "Other": 11,
}

# Spellings a caller is likely to use, mapped onto the ids above.
POLYPIZZA_CATEGORY_ALIASES = {
    "food": 0, "drink": 0, "drinks": 0,
    "weapon": 2,
    "vehicle": 3, "vehicles": 3, "transportation": 3,
    "furniture": 4, "decor": 4,
    "object": 5, "prop": 5, "props": 5,
    "plant": 6, "plants": 6,
    "animal": 7,
    "building": 8, "architecture": 8, "buildingsarchitecture": 8,
    "person": 9, "character": 9, "characters": 9, "people": 9,
    "scene": 10, "scenes": 10, "level": 10, "levels": 10,
}


def _polypizza_normalize(value):
    """Fold a human-written filter value down to comparable characters."""
    return "".join(ch for ch in str(value).lower() if ch.isalnum())


def _polypizza_category_id(category):
    """Coerce a category name or id into the numeric id the API expects."""
    if category is None or category == "":
        return None
    if isinstance(category, bool):
        raise ValueError("Poly Pizza category must be a name or an id in 0-11")
    if isinstance(category, int) or (isinstance(category, str) and category.strip().lstrip("-").isdigit()):
        value = int(category)
        if not 0 <= value <= 11:
            raise ValueError(f"Poly Pizza category id {value} is out of range (valid ids are 0-11)")
        return value

    key = _polypizza_normalize(category)
    for name, value in POLYPIZZA_CATEGORIES.items():
        if _polypizza_normalize(name) == key:
            return value
    if key in POLYPIZZA_CATEGORY_ALIASES:
        return POLYPIZZA_CATEGORY_ALIASES[key]
    raise ValueError(
        f"Unknown Poly Pizza category {category!r}. Valid categories: "
        + ", ".join(POLYPIZZA_CATEGORIES)
    )


def _polypizza_licence_id(licence):
    """Coerce a licence name or id into the numeric id the API expects."""
    if licence is None or licence == "":
        return None
    if isinstance(licence, bool):
        raise ValueError("Poly Pizza licence must be 'CC0', 'CC-BY', 0 or 1")
    if isinstance(licence, int) or (isinstance(licence, str) and licence.strip().lstrip("-").isdigit()):
        value = int(licence)
        if value not in (0, 1):
            raise ValueError(f"Poly Pizza licence id {value} is invalid (0 = CC-BY, 1 = CC0)")
        return value

    key = _polypizza_normalize(licence)
    if key.startswith("ccby"):
        return 0
    if key.startswith("cc0") or key == "publicdomain":
        return 1
    raise ValueError(f"Unknown Poly Pizza licence {licence!r}. Use 'CC0' or 'CC-BY'.")


<<<<<<< HEAD
@mcp.tool()
async def get_polypizza_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Poly Pizza integration is enabled in Blender.
    Returns a message indicating whether Poly Pizza features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_polypizza_status")
        enabled = result.get("enabled", False)
        message = result.get("message", "")
        if enabled:
            message += (
                " Poly Pizza is good at stylised, low-poly game assets. Everything is free under "
                "CC0 or CC-BY, and models are far lighter geometry than Sketchfab's."
            )
        return message
    except Exception as e:
        logger.error(f"Error checking Poly Pizza status: {str(e)}")
        return f"Error checking Poly Pizza status: {str(e)}"

@mcp.tool()
async def search_polypizza_models(
=======

@telemetry_tool("search_polypizza_models")
async def _search_polypizza(
>>>>>>> upstream/main
    ctx: Context,
    query: str = "",
    category: str | None = None,
    licence: str | None = None,
    animated: bool = False,
    limit: int = 20, user_prompt: str = "") -> str:
<<<<<<< HEAD
    """
    Search for models on Poly Pizza with optional filtering.

    Parameters:
    - query: Text to search for. May be left empty if at least one filter is given.
    - category: Optional category name, e.g. "Animals", "Furniture & Decor", "Transport",
                "Nature", "Buildings", "People & Characters", "Food & Drink", "Weapons",
                "Clutter", "Objects", "Scenes & Levels", "Other"
    - licence: Optional licence filter, either "CC0" (no credit required) or "CC-BY"
               (credit required)
    - animated: When True, return only animated models (default False)
    - limit: Maximum number of results to return (default 20, the API caps it at 32)

    Returns a formatted list of matching models, with licence and triangle count on
    every row so a low-poly, permissively licensed asset can be picked without a
    second call.
    """
=======
    """search_assets(source="polypizza"): matching models with licence and triangle count."""
>>>>>>> upstream/main
    try:
        try:
            category_id = _polypizza_category_id(category)
            licence_id = _polypizza_licence_id(licence)
        except ValueError as e:
            return f"Error: {str(e)}"

        if not (query or "").strip() and category_id is None and licence_id is None and not animated:
            return (
                "Error: Poly Pizza needs a search keyword or at least one filter "
                "(category, licence, or animated=True)."
            )

        blender = get_blender_connection()
        logger.info(
            f"Searching Poly Pizza models with query: {query}, category: {category}, "
            f"licence: {licence}, animated: {animated}, limit: {limit}"
        )
        result = blender.send_command("search_polypizza_models", {
            "query": query,
            "category": category_id,
            "licence": licence_id,
            "animated": animated,
            "limit": limit
        })

        if result is None:
            logger.error("Received None result from Poly Pizza search")
            return "Error: Received no response from Poly Pizza search"

        if "error" in result:
            logger.error(f"Error from Poly Pizza search: {result['error']}")
            return f"Error: {result['error']}"

        models = result.get("results", []) or []
        if not models:
            described = query or "the requested filters"
            return f"No models found matching '{described}'"

        total = result.get("total", len(models))
        formatted_output = f"Found {len(models)} models (of {total} total) matching '{query or 'the given filters'}':\n\n"
        credit_note = (
<<<<<<< HEAD
            "CC-BY models must be credited. download_polypizza_model() stores the required "
=======
            "CC-BY models must be credited. import_asset stores the required "
>>>>>>> upstream/main
            "attribution string on the imported object as a custom property.\n"
        )
        blocks = {}
        options = []

        for model in models:
            if model is None:
                continue

            model_name = model.get("Title", "Unnamed model")
            model_id = model.get("ID", "Unknown ID")
            licence_label = model.get("Licence") or "Unknown"
            tri_count = model.get("Tri Count")
            block = f"- {model_name} (ID: {model_id})\n"
            block += f"  Author: {model.get('Creator') or 'Unknown author'}\n"
            block += f"  Licence: {licence_label}\n"
            block += f"  Tri count: {tri_count if tri_count else 'Unknown'}\n"
            block += f"  Category: {model.get('Category') or 'Unknown'}\n"
            block += f"  Animated: {'Yes' if model.get('Animated') else 'No'}\n"
            formatted_output += block + "\n"
            blocks[model_id] = f"{block}\n{credit_note}"
            thumbnail = model.get("Thumbnail")
            options.append(PickerOption(
                id=model_id,
                title=model_name,
                description=" · ".join(filter(None, [
                    model.get("Creator"), licence_label, f"{tri_count} tris" if tri_count else None,
                ])),
                thumbnail=thumbnail if isinstance(thumbnail, str) and thumbnail.startswith("https://") else None,
            ))

        formatted_output += credit_note

        described = query or category or "your scene"
        picked = await pick_asset(ctx, f"Pick a Poly Pizza model for: {described}", "Model", options)
        return picked_reply("Poly Pizza", picked, blocks, formatted_output) if picked else formatted_output
    except Exception as e:
        logger.error(f"Error searching Poly Pizza models: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error searching Poly Pizza models: {str(e)}"


<<<<<<< HEAD
@mcp.tool()
async def download_polypizza_model(
=======
@trajectory_tool("download_polypizza_model")
async def _download_polypizza(
>>>>>>> upstream/main
    ctx: Context,
    model_id: str,
    normalize_size: bool = False,
    target_size: float = 1.0, user_prompt: str = "") -> str:
<<<<<<< HEAD
    """
    Download and import a Poly Pizza model by its ID.

    Poly Pizza models come from the rescued Google Poly archive, so their scale and
    origins are arbitrary. Pass normalize_size=True with a real-world target_size
    unless you have a reason not to.

    Parameters:
    - model_id: The Poly Pizza model ID (obtained from search_polypizza_models)
    - normalize_size: If True, scale the model so its largest dimension equals target_size
    - target_size: The target size in Blender units/meters for the largest dimension.
                  Examples:
                  - Chair: target_size=1.0 (1 meter tall)
                  - Table: target_size=0.75 (75cm tall)
                  - Car: target_size=4.5 (4.5 meters long)
                  - Person: target_size=1.7 (1.7 meters tall)
                  - Small object (cup, phone): target_size=0.1 to 0.3

    Returns a message with import details including object names, dimensions, bounding
    box, and the attribution string, which is also written onto each imported root
    object as the custom properties polypizza_attribution, polypizza_id and
    polypizza_licence.
    """
=======
    """import_asset(source="polypizza"): import a model and record its attribution on it."""
>>>>>>> upstream/main
    try:
        blender = get_blender_connection()
        logger.info(
            f"Downloading Poly Pizza model: {model_id}, normalize_size={normalize_size}, "
            f"target_size={target_size}"
        )

        result = blender.send_command("download_polypizza_model", {
            "model_id": model_id,
            "normalize_size": normalize_size,
            "target_size": target_size
        })

        if result is None:
            logger.error("Received None result from Poly Pizza download")
            return "Error: Received no response from Poly Pizza download request"

        if "error" in result:
            logger.error(f"Error from Poly Pizza download: {result['error']}")
            return f"Error: {result['error']}"

        if result.get("success"):
            imported_objects = result.get("imported_objects", [])
            object_names = ", ".join(imported_objects) if imported_objects else "none"

            output = f"Successfully imported model.\n"
            output += f"Created objects: {object_names}\n"

            if result.get("title"):
                output += f"Title: {result['title']}\n"

            if result.get("tri_count"):
                output += f"Tri count: {result['tri_count']}\n"

            # Add dimension info if available
            if result.get("dimensions"):
                dims = result["dimensions"]
                output += f"Dimensions (X, Y, Z): {dims[0]:.3f} x {dims[1]:.3f} x {dims[2]:.3f} meters\n"

            # Add bounding box info if available
            if result.get("world_bounding_box"):
                bbox = result["world_bounding_box"]
                output += f"Bounding box: min={bbox[0]}, max={bbox[1]}\n"

            # Add normalization info if applied
            if result.get("normalized"):
                scale = result.get("scale_applied", 1.0)
                output += f"Size normalized: scale factor {scale:.6f} applied (target size: {target_size}m)\n"

            output += f"Licence: {result.get('licence') or 'Unknown'}\n"
            if result.get("attribution"):
                output += f"Attribution: {result['attribution']}\n"
                output += (
                    "Stored on the imported object as polypizza_attribution. Surface it to the user "
                    "if the licence is CC-BY.\n"
                )

            return output
        else:
            return f"Failed to download model: {result.get('message', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Error downloading Poly Pizza model: {str(e)}")
        import traceback
        logger.error(traceback.format_exc())
        return f"Error downloading Poly Pizza model: {str(e)}"


<<<<<<< HEAD
@mcp.tool()
async def generate_hyper3d_model_via_text(
    ctx: Context,
    text_prompt: str,
    bbox_condition: list[float]=None, user_prompt: str = "") -> str:
    """
    Generate 3D asset using Hyper3D by giving description of the desired asset, and import the asset into Blender.
    The 3D asset has built-in materials.
    The generated model has a normalized size, so re-scaling after generation can be useful.

    Parameters:
    - text_prompt: A short description of the desired model in **English**.
    - bbox_condition: Optional. If given, it has to be a list of floats of length 3. Controls the ratio between [Length, Width, Height] of the model.

    Returns a message indicating success or failure.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_rodin_job", {
            "text_prompt": text_prompt,
            "images": None,
            "bbox_condition": _process_bbox(bbox_condition),
        })
        succeed = result.get("submit_time", False)
        if succeed:
            return json.dumps({
                "task_uuid": result["uuid"],
                "subscription_key": result["jobs"]["subscription_key"],
            })
        else:
            return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
async def generate_hyper3d_model_via_images(
    ctx: Context,
    input_image_paths: list[str]=None,
    input_image_urls: list[str]=None,
    bbox_condition: list[float]=None, user_prompt: str = "") -> str:
    """
    Generate 3D asset using Hyper3D by giving images of the wanted asset, and import the generated asset into Blender.
    The 3D asset has built-in materials.
    The generated model has a normalized size, so re-scaling after generation can be useful.
    
    Parameters:
    - input_image_paths: The **absolute** paths of input images. Even if only one image is provided, wrap it into a list. Required if Hyper3D Rodin in MAIN_SITE mode.
    - input_image_urls: The URLs of input images. Even if only one image is provided, wrap it into a list. Required if Hyper3D Rodin in FAL_AI mode.
    - bbox_condition: Optional. If given, it has to be a list of ints of length 3. Controls the ratio between [Length, Width, Height] of the model.

    Only one of {input_image_paths, input_image_urls} should be given at a time, depending on the Hyper3D Rodin's current mode.
    Returns a message indicating success or failure.
    """
    if input_image_paths is not None and input_image_urls is not None:
        return f"Error: Conflict parameters given!"
    if input_image_paths is None and input_image_urls is None:
        return f"Error: No image given!"
    if input_image_paths is not None:
        if not all(os.path.exists(i) for i in input_image_paths):
            return "Error: not all image paths are valid!"
        images = []
        for path in input_image_paths:
            with open(path, "rb") as f:
                images.append(
                    (Path(path).suffix, base64.b64encode(f.read()).decode("ascii"))
                )
    elif input_image_urls is not None:
        if not all(urlparse(i) for i in input_image_paths):
            return "Error: not all image URLs are valid!"
        images = input_image_urls.copy()
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_rodin_job", {
            "text_prompt": None,
            "images": images,
            "bbox_condition": _process_bbox(bbox_condition),
        })
        succeed = result.get("submit_time", False)
        if succeed:
            return json.dumps({
                "task_uuid": result["uuid"],
                "subscription_key": result["jobs"]["subscription_key"],
            })
        else:
            return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
async def poll_rodin_job_status(
    ctx: Context,
    subscription_key: str=None,
    request_id: str=None,
):
    """
    Check if the Hyper3D Rodin generation task is completed.

    For Hyper3D Rodin mode MAIN_SITE:
        Parameters:
        - subscription_key: The subscription_key given in the generate model step.

        Returns a list of status. The task is done if all status are "Done".
        If "Failed" showed up, the generating process failed.
        This is a polling API, so only proceed if the status are finally determined ("Done" or "Canceled").

    For Hyper3D Rodin mode FAL_AI:
        Parameters:
        - request_id: The request_id given in the generate model step.

        Returns the generation task status. The task is done if status is "COMPLETED".
        The task is in progress if status is "IN_PROGRESS".
        If status other than "COMPLETED", "IN_PROGRESS", "IN_QUEUE" showed up, the generating process might be failed.
        This is a polling API, so only proceed if the status are finally determined ("COMPLETED" or some failed state).
    """
    try:
        blender = get_blender_connection()
        kwargs = {}
        if subscription_key:
            kwargs = {
                "subscription_key": subscription_key,
            }
        elif request_id:
            kwargs = {
                "request_id": request_id,
            }
        result = blender.send_command("poll_rodin_job_status", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
async def import_generated_asset(
    ctx: Context,
    name: str,
    task_uuid: str=None,
    request_id: str=None,
):
    """
    Import the asset generated by Hyper3D Rodin after the generation task is completed.

    Parameters:
    - name: The name of the object in scene
    - task_uuid: For Hyper3D Rodin mode MAIN_SITE: The task_uuid given in the generate model step.
    - request_id: For Hyper3D Rodin mode FAL_AI: The request_id given in the generate model step.

    Only give one of {task_uuid, request_id} based on the Hyper3D Rodin Mode!
    Return if the asset has been imported successfully.
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "name": name
        }
        if task_uuid:
            kwargs["task_uuid"] = task_uuid
        elif request_id:
            kwargs["request_id"] = request_id
        result = blender.send_command("import_generated_asset", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hyper3D task: {str(e)}")
        return f"Error generating Hyper3D task: {str(e)}"

@mcp.tool()
def get_hunyuan3d_status(ctx: Context, user_prompt: str = "") -> str:
    """
    Check if Hunyuan3D integration is enabled in Blender.
    Returns a message indicating whether Hunyuan3D features are available.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("get_hunyuan3d_status")
        message = result.get("message", "")
        return message
    except Exception as e:
        logger.error(f"Error checking Hunyuan3D status: {str(e)}")
        return f"Error checking Hunyuan3D status: {str(e)}"
    
@mcp.tool()
async def generate_hunyuan3d_model(
    ctx: Context,
    text_prompt: str = None,
    input_image_url: str = None, user_prompt: str = "") -> str:
    """
    Generate 3D asset using Hunyuan3D by providing either text description, image reference, 
    or both for the desired asset, and import the asset into Blender.
    The 3D asset has built-in materials.
    
    Parameters:
    - text_prompt: (Optional) A short description of the desired model in English/Chinese.
    - input_image_url: (Optional) The local or remote url of the input image. Accepts None if only using text prompt.

    Returns: 
    - When successful, returns a JSON with job_id (format: "job_xxx") indicating the task is in progress
    - When the job completes, the status will change to "DONE" indicating the model has been imported
    - Returns error message if the operation fails
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("create_hunyuan_job", {
            "text_prompt": text_prompt,
            "image": input_image_url,
        })
        if "JobId" in result.get("Response", {}):
            job_id = result["Response"]["JobId"]
            formatted_job_id = f"job_{job_id}"
            return json.dumps({
                "job_id": formatted_job_id,
            })
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Error generating Hunyuan3D task: {str(e)}")
        return f"Error generating Hunyuan3D task: {str(e)}"
    
@mcp.tool()
def poll_hunyuan_job_status(
    ctx: Context,
    job_id: str=None,
):
    """
    Check if the Hunyuan3D generation task is completed.

    For Hunyuan3D:
        Parameters:
        - job_id: The job_id given in the generate model step.

        Returns the generation task status. The task is done if status is "DONE".
        The task is in progress if status is "RUN".
        If status is "DONE", returns ResultFile3Ds with one or more downloadable model URLs.
        Prefer a .glb URL when present (self-contained with materials); otherwise use a .zip/.obj asset URL.
        This is a polling API, so only proceed if the status are finally determined ("DONE" or some failed state).
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "job_id": job_id,
        }
        result = blender.send_command("poll_hunyuan_job_status", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hunyuan3D task: {str(e)}")
        return f"Error generating Hunyuan3D task: {str(e)}"

@mcp.tool()
async def import_generated_asset_hunyuan(
    ctx: Context,
    name: str,
    zip_file_url: str,
):
    """
    Import the asset generated by Hunyuan3D after the generation task is completed.

    Parameters:
    - name: The name of the object in scene
    - zip_file_url: A model URL from ResultFile3Ds. Prefer a .glb URL when available; .zip/.obj URLs still work as a fallback.

    Return if the asset has been imported successfully.
    """
    try:
        blender = get_blender_connection()
        kwargs = {
            "name": name
        }
        if zip_file_url:
            kwargs["zip_file_url"] = zip_file_url
        result = blender.send_command("import_generated_asset_hunyuan", kwargs)
        return result
    except Exception as e:
        logger.error(f"Error generating Hunyuan3D task: {str(e)}")
        return f"Error generating Hunyuan3D task: {str(e)}"


@mcp.tool()
async def export_scene(
    ctx: Context,
    filepath: str,
    format: str = "glb",
    object_names: list[str] = None,
    selection_only: bool = False,
    apply_modifiers: bool = True,
    user_prompt: str = "",
) -> str:
    """
    Export the whole scene, the current selection, or named objects to a GLB or FBX file on disk,
    so another application (a game engine, a viewer, a converter) can pick it up.

    Parameters:
    - filepath: Absolute path of the file to write (.glb or .fbx). Parent folders are created.
    - format: "glb" (default; keeps PBR materials, emission, skins, shape keys, animation) or "fbx".
    - object_names: Export only these objects (children included). Omit for selection_only or the whole scene.
    - selection_only: Export what is currently selected in Blender (ignored when object_names is given).
    - apply_modifiers: Bake modifiers on export. Use false for rigged / shape-key meshes.

    Returns JSON with path, bytes, selection_only and the exported object names.
    """
    try:
        blender = get_blender_connection()
        result = blender.send_command("export_scene", {
            "filepath": filepath,
            "format": format,
            "object_names": object_names,
            "selection_only": selection_only,
            "apply_modifiers": apply_modifiers,
        })
        return json.dumps(result) if isinstance(result, dict) else result
    except Exception as e:
        logger.error(f"Error exporting scene: {str(e)}")
        return f"Error exporting scene: {str(e)}"
=======
TRIPO_UNAVAILABLE = generation.TRIPO_UNAVAILABLE

>>>>>>> upstream/main


@mcp.tool()
def record_trajectory_feedback(
    ctx: Context,
    feedback: str,
    correction_text: str | None = None,
    step_index: int | None = None,
    user_prompt: str = "",
) -> str:
    """
    DEPRECATED: Telemetry/analytics not in this fork.

<<<<<<< HEAD
    Trajectory capture does not exist here, so there is no step to attach
    feedback to. Kept as a no-op so existing client prompts that reference it
    still resolve.
=======
    Call it when the user reacts to a result: "accept" when they keep it ("looks good"),
    "reject" or "undo" when they reject it or ask to undo, and "correction" with their words
    as correction_text when they correct you ("too dark", "make it taller").

    Parameters:
    - feedback: One of accept | reject | undo | correction
    - correction_text: Optional free-text correction or follow-up (especially for correction)
    - step_index: Optional 0-based step index; defaults to the last recorded step
    - user_prompt: Optional goal/prompt context for the feedback row
>>>>>>> upstream/main
    """
    return "Not recorded: telemetry and analytics are not part of this fork."


# The model-facing tool surface. Each tool covers a job the model can't do
# with execute_blender_code alone: seeing the scene (look), paid and keyed
# services (generate_3d, search_assets, import_asset), and craft knowledge it
# loads only when needed (get_guide). The per-provider functions above are
# their building blocks and no longer registered as tools.

LOOK_MODES = ("viewport", "camera", "angles", "frames")
LOOK_ANGLES = ("front", "back", "left", "right", "top", "three_quarter")
LOOK_SHADING = ("solid", "material", "rendered", "wireframe", "xray")
# Modes before the shading/stats split, so a model trained on them gets pointed the right way.
LOOK_RETIRED = {
    "topology": 'Use shading="wireframe" to see edges, and get_scene_info(fields=["topology"]) for counts.',
    "rig": 'Use shading="xray" to see bones, and get_scene_info(fields=["weights"]) for weighting.',
    "image": 'Pass image= on its own, e.g. look(image="Render Result").',
}

<<<<<<< HEAD
    1. First use the following tools to verify if the following integrations are enabled:
        1. PolyHaven
            Use get_polyhaven_status() to verify its status
            If PolyHaven is enabled:
            - For objects/models: Use download_polyhaven_asset() with asset_type="models"
            - For materials/textures: Use download_polyhaven_asset() with asset_type="textures"
            - For environment lighting: Use download_polyhaven_asset() with asset_type="hdris"
        2. Sketchfab
            Sketchfab is good at Realistic models, and has a wider variety of models than PolyHaven.
            Use get_sketchfab_status() to verify its status
            If Sketchfab is enabled:
            - For objects/models: First search using search_sketchfab_models() with your query
            - Then download specific models using download_sketchfab_model() with the UID
            - Note that only downloadable models can be accessed, and API key must be properly configured
            - Sketchfab has a wider variety of models than PolyHaven, especially for specific subjects
        3. Poly Pizza
            Poly Pizza is best for stylised, low-poly game assets (it includes the rescued Google Poly archive).
            Everything on it is free under CC0 or CC-BY, every model is a single self-contained GLB, and the
            geometry is much lighter than Sketchfab's - prefer it when the scene wants a consistent stylised
            look, or when many props are needed without heavy meshes.
            Use get_polypizza_status() to verify its status
            If Poly Pizza is enabled:
            - For objects/models: First search using search_polypizza_models(), optionally filtering by
              category (e.g. "Animals", "Furniture & Decor"), licence ("CC0" or "CC-BY"), or animated=True
            - Then import a specific model using download_polypizza_model() with its ID, passing
              normalize_size=True and a real-world target_size: Poly Pizza models come from the Google Poly
              archive and their scale and origins are arbitrary
            - About 69% of the catalogue is CC-BY, which REQUIRES crediting the creator. The ready-formatted
              attribution string is returned by download_polypizza_model() and is also stored on the imported
              object as the custom property polypizza_attribution, so tell the user about it when the model
              is CC-BY. Filter with licence="CC0" if you want models that need no credit.
        4. Hyper3D(Rodin)
            Hyper3D Rodin is good at generating 3D models for single item.
            So don't try to:
            1. Generate the whole scene with one shot
            2. Generate ground using Hyper3D
            3. Generate parts of the items separately and put them together afterwards
=======
>>>>>>> upstream/main

def _look_caption(info: dict) -> str:
    mode = info["mode"]
    if mode == "image":
        w0, h0 = info["original_size"]
        return f"Image '{info['image']}', {w0}x{h0}, shown at {info['width']}x{info['height']}."
    parts = []
    if mode == "angles":
        parts.append("Tiles left to right, top to bottom: " + ", ".join(info.get("views", [])) + ".")
    if mode == "frames":
        parts.append("Frames left to right, top to bottom: " + ", ".join(map(str, info.get("frames", []))) + ".")
    if mode == "camera":
        parts.append(f"Through camera '{info.get('camera')}'.")
    if mode != "viewport":
        size = " x ".join(f"{v:g}" for v in info.get("size", []))
        parts.append(f"Framed {info.get('targets', 0)} objects, {size} m across, centred at {info.get('center')}.")
    return " ".join(parts)


@mcp.tool(meta={"ui": {"resourceUri": VIEWPORT_URI}})
@telemetry_tool("look")
async def look(
    ctx: Context,
    mode: str | None = None,
    target: list[str] | None = None,
    views: list[str | list[float]] | None = None,
    distance: float | None = None,
    shading: str | None = None,
    frames: list[int] | None = None,
    frame_count: int = 6,
    view: str | list[float] | None = None,
    image: str | None = None,
    max_size: int = 768,
    user_prompt: str = "",
) -> CallToolResult:
    """
    See the scene as one image. For counts, sizes and positions, use get_scene_info.

    Choose where from (mode) and how it's drawn (shading):
    - mode: viewport (what the user sees; default), camera (through the scene camera, at the
      render aspect), angles (the target from several sides, auto-framed; default front, right,
      top, three_quarter), frames (a strip over the animation).
    - shading: solid, material, rendered (EEVEE and Workbench only), wireframe (edges over a
      plain surface), xray (see-through, bones in front). Default: the viewport's.
    - image: Instead of the scene, show this image: "Render Result" after a render, another
      image in the file, or a file path.

    Parameters:
    - target: Object names to frame (children included). Default: every visible object.
    - views: For angles: up to 6 of front, back, left, right, top, three_quarter, or [x, y, z]
      directions from the target towards the eye ([0, -1, 0.2] is front, slightly above).
    - distance: Metres from the target's centre to the eye, for angles and frames views. Default:
      far enough to fit it; closer for detail or to stand inside a room.
    - frames / frame_count: For frames: explicit frame numbers, or how many to sample (2-12).
    - view: For frames: "camera", an angle name or an [x, y, z] direction; default the viewport.
    - max_size: Longest side in pixels (default 768). Images stay in the conversation, so go
      smaller for quick checks and larger only to read fine detail.
    - user_prompt: The user's own words describing what they want, quoted verbatim.

    Every setting changed to take the picture is restored afterwards.
    """
    if mode in LOOK_RETIRED:
        return _app_error(f"There is no {mode} mode. {LOOK_RETIRED[mode]}")
    if mode is not None and mode not in LOOK_MODES:
        return _app_error(f"Unknown mode {mode!r}. Use one of: {', '.join(LOOK_MODES)}")
    if image is not None:
        if mode is not None:
            return _app_error("image= shows an image instead of the scene; leave mode unset.")
        mode = "image"
    mode = mode or "viewport"
    if shading is not None and shading not in LOOK_SHADING:
        return _app_error(f"Unknown shading {shading!r}. Use one of: {', '.join(LOOK_SHADING)}")
    for v in (views or []) + ([view] if view is not None and view != "camera" else []):
        if isinstance(v, str) and v not in LOOK_ANGLES:
            return _app_error(f"Unknown view {v!r}. Use one of {', '.join(LOOK_ANGLES)} or an [x, y, z] direction.")
        if not isinstance(v, str) and (len(v) != 3 or not any(v)):
            return _app_error(f"A view direction is three numbers, not all zero; got {v!r}.")
    args = {"mode": mode, "target": target, "views": views, "distance": distance, "shading": shading,
            "frames": frames, "frame_count": frame_count, "view": view, "image": image,
            "max_size": max(200, min(int(max_size or 768), 2000))}

    def native():
        return _viewport_screenshot(ctx, max_size=max_size, user_prompt=user_prompt)

    def scripted():
        return _look_via_script(args)

    # The plain viewport has a native command; everything else is a script.
    # Each falls back to the other, since old addons may have only one of them.
    first, second = (native, scripted) if mode == "viewport" and not shading else (scripted, native)
    try:
        return first()
    except _LookRefused as e:
        return _app_error(str(e))
    except Exception as e:
        reason = str(e)
    if mode == "image":
        # The viewport is no stand-in for the image that was asked for.
        hint = f" {ADDON_UPDATE_HINT}" if _addon_outdated() and ADDON_UPDATE_HINT not in reason else ""
        return _app_error(f"Couldn't show the image: {reason}{hint}")
    try:
        result = second()
    except Exception as e:
        hint = f" {ADDON_UPDATE_HINT}" if _addon_outdated() and ADDON_UPDATE_HINT not in reason else ""
        return _app_error(f"Couldn't capture the view: {reason}{hint}")
    if second is native:
        note = f"look(mode=\"{mode}\") isn't available here ({reason}), so this is the plain viewport."
        result.content.append(TextContent(type="text", text=note))
    return result


class _LookRefused(Exception):
    """A mistake in the request (a missing object, no camera): report it, don't fall back."""


def _look_via_script(args: dict) -> CallToolResult:
    path = os.path.join(tempfile.gettempdir(), f"blender_look_{os.getpid()}.png")
    info = _run_script(blender_scripts.LOOK, {**args, "filepath": path})
    if info.get("error"):
        raise _LookRefused(info["error"])
    with open(path, "rb") as f:
        png = f.read()
    os.remove(path)
    return CallToolResult(content=[_png_content(png), TextContent(type="text", text=_look_caption(info))])


def _generation_send(command: str, params: dict):
    try:
        return get_blender_connection().send_command(command, params)
    except Exception as e:
        if _addon_lacks(e):
            if "tripo" in command:
                raise generation.GenerationError(TRIPO_UNAVAILABLE)
            label = "Hunyuan3D" if "hunyuan" in command else "Hyper3D Rodin"
            raise generation.GenerationError(missing_feature(label, label))
        raise


def _own_key_generators(blender: BlenderConnection) -> dict[str, bool]:
    enabled = {}
    for name, command in (("hunyuan3d", "get_hunyuan3d_status"), ("hyper3d", "get_hyper3d_status")):
        try:
            enabled[name] = bool(blender.send_command(command).get("enabled"))
        except Exception:
            enabled[name] = False
    return enabled


async def _generation_reply(ctx: Context, job: "generation.Job", wait_seconds: float, note: str = "") -> str:
    async def progress(done, total):
        await ctx.report_progress(done, total)

    imported, detail = await generation.wait_and_import(_generation_send, job, wait_seconds, progress)
    if not imported:
        return (f"Still generating ({detail}). Call generate_3d(job=\"{job.handle}\", name=\"{job.name}\") "
                f"to keep waiting; it imports the model when it's ready. Don't start a new generation.{note}")
    reply = f"Generated and imported '{job.name}' with {job.provider}."
    try:
        bounds = _run_script(blender_scripts.BOUNDS, {"names": [job.name]})
    except Exception:
        bounds = []
    for b in bounds:
        lo, hi = b["world_bounding_box"]
        reply += (f" world_bounding_box min {lo}, max {hi} (size {b['size']} m). Generated models have "
                  "arbitrary scale and facing: scale it to real size, put its lowest point on the ground, "
                  "rotate it to face the right way, then look(mode=\"angles\", target=[\"" + b["name"] + "\"]).")
    return reply + note


@mcp.tool()
@trajectory_tool("generate_3d")
async def generate_3d(
    ctx: Context,
    prompt: str | None = None,
    image: str | None = None,
    name: str | None = None,
    provider: str = "auto",
    quality: str | None = None,
    bbox_condition: list[float] | None = None,
    job: str | None = None,
    wait_seconds: int = 50,
    user_prompt: str = "",
) -> str:
    """
    Make one new textured 3D model from a text prompt or an image, and import it.

    One object per call: not a whole scene, the ground, or parts to assemble. It arrives at
    arbitrary scale and facing. Each call can cost the user money or a monthly generation, so
    duplicate a generated object for repeats.

    Waits up to wait_seconds, then imports. Generation usually takes 1-3 minutes: if it isn't done
    in time you get a job handle; call generate_3d(job=..., name=...) again to keep waiting.

    Parameters:
    - prompt: Short English description of one object ("weathered wooden treasure chest").
    - image: Instead of a prompt: an absolute image file path or an http(s) URL. Images attached in
      chat can't be passed: ask the user for a path or URL, don't fall back to text without asking.
    - name: Object name in the scene. Defaults to one made from the prompt.
    - provider: auto (default), tripo, hunyuan3d or hyper3d. Auto prefers the user's MCP for
      Blender Premium generators, then their own API keys.
    - quality: "standard" or "high" (Premium). Omit for the user's default; "high" only when the
      user asks for more detail.
    - bbox_condition: hyper3d only: [length, width, height] proportions.
    - job: A handle from an earlier call, to resume waiting for it.
    - user_prompt: The user's own words describing what they want, quoted verbatim.
    """
    if quality not in (None, "standard", "high"):
        return "Error: quality must be 'standard' or 'high'"
    wait_seconds = max(10, min(int(wait_seconds or 50), 600))
    try:
        if job:
            return await _generation_reply(ctx, generation.Job.parse(job, name or "Generated"), wait_seconds)
        if bool(prompt) == bool(image):
            return "Error: give exactly one of prompt or image."
        blender = get_blender_connection()
        premium = _premium_generators(blender)
        chosen, is_premium = generation.choose_provider(
            provider, premium, {} if premium else _own_key_generators(blender))
        note = "" if is_premium else premium_hint_once(ctx, {"mode": None})
        started = generation.submit(
            _generation_send, chosen, name or generation.default_name(prompt), prompt, image, quality,
            bbox_condition, supports_quality=(_addon_protocol() or 0) >= 11)
        if isinstance(started, str):
            return started + note
        return await _generation_reply(ctx, started, wait_seconds, note)
    except generation.GenerationError as e:
        return f"Error: {e}"
    except Exception as e:
        logger.error(f"Error generating model: {e}")
        return f"Error generating model: {e}"


ASSET_SOURCES = ("polyhaven", "sketchfab", "polypizza")


def _unavailable(source: str, e: Exception, action: str) -> str:
    if _addon_lacks(e):
        label = {"polyhaven": "Poly Haven", "sketchfab": "Sketchfab", "polypizza": "Poly Pizza"}[source]
        return missing_feature(f"{label} {action}", label)
    return f"Error: {e}"


def _preview_images(source: str, listing: str, count: int) -> list[ImageContent]:
    """Thumbnails of the first `count` results, read off the listing's ids."""
    pattern = {"polyhaven": r"\(ID: ([^)]+)\)", "sketchfab": r"\(UID: ([^)]+)\)"}.get(source)
    if not pattern or count <= 0:
        return []
    command = {"polyhaven": "get_polyhaven_asset_preview", "sketchfab": "get_sketchfab_model_preview"}[source]
    key = {"polyhaven": "asset_id", "sketchfab": "uid"}[source]
    images = []
    for ident in re.findall(pattern, listing)[:count]:
        try:
            result = get_blender_connection().send_command(command, {key: ident}, read_only=True)
            images.append(ImageContent(type="image", data=result["image_data"],
                                       mimeType=f"image/{result.get('format', 'png').replace('jpg', 'jpeg')}"))
        except Exception as e:
            logger.debug(f"Preview of {ident} failed: {e}")
    return images


@mcp.tool()
async def search_assets(
    ctx: Context,
    source: str,
    query: str = "",
    asset_type: str = "all",
    category: str | None = None,
    attributes: dict | None = None,
    min_size_m: float | None = None,
    licence: str | None = None,
    animated: bool = False,
    limit: int = 20,
    previews: int = 0,
    user_prompt: str = "",
):
    """
    Search a library of existing assets. The sources:
    - polyhaven: HDRIs, PBR textures and realistic models, all CC0. Search understands intent and
      synonyms ("couch" finds sofas).
    - sketchfab: a large catalogue of user-made models, realistic and specific ones included;
      licences and face counts vary per model.
    - polypizza: stylised low-poly models, CC0 or CC-BY (credit the creator).

    Parameters:
    - source: polyhaven, sketchfab or polypizza.
    - query: What you're looking for, in plain words.
    - asset_type: polyhaven only: hdris, textures, models or all.
    - category: Optional. polyhaven: a category path ("Metal/Sheet & Corrugated"). sketchfab:
      comma-separated categories. polypizza: e.g. "Furniture & Decor", "Nature", "Animals".
    - attributes: polyhaven only: filters like {"weather": "clear"}; an unknown key errors with the
      valid ones.
    - min_size_m: polyhaven only: minimum real-world size in metres. Use 2+ for walls, floors and
      ground so textures don't visibly repeat.
    - licence: polypizza only: "CC0" or "CC-BY".
    - animated: polypizza only: animated models only.
    - limit: Number of results.
    - previews: Attach thumbnails of the first N results (max 6; polyhaven and sketchfab). Cheaper
      than importing the wrong asset.
    - user_prompt: The user's own words describing what they want, quoted verbatim.

    Results include each asset's id; pass it to import_asset.
    """
    source = (source or "").lower()
    if source not in ASSET_SOURCES:
        return f"Error: source must be one of {', '.join(ASSET_SOURCES)}"
    limit = max(1, min(int(limit or 20), 50))
    try:
        if source == "polyhaven":
            listing = await _search_polyhaven(
                ctx, query=query or None, asset_type=asset_type, category=category, attributes=attributes,
                min_size_m=min_size_m, limit=limit, user_prompt=user_prompt)
        elif source == "sketchfab":
            if not query:
                return "Error: sketchfab needs a query."
            listing = await _search_sketchfab(
                ctx, query=query, categories=category, count=limit, user_prompt=user_prompt)
        else:
            listing = await _search_polypizza(
                ctx, query=query, category=category, licence=licence, animated=animated, limit=limit,
                user_prompt=user_prompt)
    except Exception as e:
        return _unavailable(source, e, "search")
    if listing.lower().startswith("error") and _addon_lacks(listing):
        return _unavailable(source, Exception(listing), "search")
    images = _preview_images(source, listing, max(0, min(int(previews or 0), 6)))
    if not images:
        return listing
    return CallToolResult(content=[TextContent(type="text", text=listing), *images])


@mcp.tool()
async def import_asset(
    ctx: Context,
    source: str,
    id: str,
    asset_type: str | None = None,
    target_size: float | None = None,
    apply_to: list[str] | None = None,
    resolution: str = "1k",
    file_format: str | None = None,
    user_prompt: str = "",
) -> str:
    """
    Download an asset found with search_assets and bring it into the scene.

    Parameters:
    - source: polyhaven, sketchfab or polypizza.
    - id: The asset's id (UID for sketchfab) from search_assets.
    - asset_type: polyhaven only, required: hdris (becomes the world lighting), textures (builds a
      PBR material) or models.
    - target_size: Size in metres of the model's largest dimension (chair 1.0, car 4.5, cup 0.12).
      Required for sketchfab, recommended for polypizza; library models come at arbitrary scale.
    - apply_to: polyhaven textures: object names to put the material on (replaces their materials).
      Without it the material is created but unused, and is lost if the file is saved.
    - resolution: polyhaven: 1k, 2k, 4k or 8k. 1k-2k for background, 4k for close-ups.
    - file_format: polyhaven, optional: hdr/exr for HDRIs, jpg/png/exr for textures.
    - user_prompt: The user's own words describing what they want, quoted verbatim.

    Afterwards check the reported bounding box, put the object on the ground, and look at it.
    """
    source = (source or "").lower()
    if source not in ASSET_SOURCES:
        return f"Error: source must be one of {', '.join(ASSET_SOURCES)}"
    reply = await _import_asset(ctx, source, id, asset_type, target_size, apply_to, resolution, file_format,
                                user_prompt)
    # The download helpers report failures as text, so an unknown command arrives inside it.
    if reply.lower().startswith("error") and _addon_lacks(reply):
        return _unavailable(source, Exception(reply), "import")
    # Old addons can fail on a newer Blender (removed node types and the like).
    if "error" in reply.lower() and _addon_outdated() and ADDON_UPDATE_HINT not in reply:
        reply += f"\n\nThe Blender addon is out of date, which may be the cause. {ADDON_UPDATE_HINT}"
    return reply


async def _import_asset(ctx, source, id, asset_type, target_size, apply_to, resolution, file_format,
                        user_prompt) -> str:
    try:
        if source == "polyhaven":
            if asset_type not in ("hdris", "textures", "models"):
                return "Error: polyhaven needs asset_type: hdris, textures or models."
            reply = await _download_polyhaven(
                ctx, asset_id=id, asset_type=asset_type, resolution=resolution, file_format=file_format,
                user_prompt=user_prompt)
            if asset_type == "textures" and apply_to and not reply.lower().startswith(("error", "failed")):
                applied = []
                for object_name in apply_to:
                    result = await _set_texture(ctx, object_name=object_name, texture_id=id, user_prompt=user_prompt)
                    applied.append(result.splitlines()[0] if result else f"{object_name}: no reply")
                applied_note = "Applied:\n" + "\n".join(applied) + "\n"
                reply = reply.replace(" " + POLYHAVEN_UNUSED_NOTE + " ", "\n" + applied_note)
                reply = reply.replace(" " + POLYHAVEN_UNUSED_NOTE, "\n" + applied_note)
            return reply
        if source == "sketchfab":
            if not target_size:
                return "Error: sketchfab needs target_size (metres, largest dimension)."
            return await _download_sketchfab(ctx, uid=id, target_size=target_size, user_prompt=user_prompt)
        return await _download_polypizza(
            ctx, model_id=id, normalize_size=bool(target_size), target_size=target_size or 1.0,
            user_prompt=user_prompt)
    except Exception as e:
        return _unavailable(source, e, "import")


# Guides (guides/, guides.py) are switched off while evals measure what the model
# does without them. To bring them back, register get_guide and the guide://
# resources here again.


# MCP Apps and OpenAI extensions. Tools marked visibility ["app"] are called by
# the host UI, never by the model, and are hidden from clients without MCP Apps.

_APP_ONLY = {"ui": {"visibility": ["app"]}}
_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_SCENE_ITEM_KINDS = ("object", "material", "collection")


def _scene_items(query: str, limit: int = 30) -> list[dict]:
    blender = get_blender_connection()
    if (_addon_protocol() or 0) >= 12:
        result = blender.send_command("list_scene_items", {"query": query, "limit": limit})
        return result.get("items", []) if isinstance(result, dict) else []
    # Older addons only list the first ten objects, and no materials.
    result = blender.send_command("get_scene_info")
    needle = query.strip().lower()
    return [
        {"kind": "object", "name": o["name"], "detail": f"{o.get('type', '').title()} object"}
        for o in (result.get("objects") or [])
        if needle in o["name"].lower()
    ]


def _scene_item_uri(kind: str, name: str) -> str:
    return f"blender://{kind}/{quote(name, safe='')}"


@mcp.tool(
    title="Mention Blender items",
    annotations=_READ_ONLY,
    meta={"openai/extensions": {"mentions/search": {}}, **_APP_ONLY},
)
async def search_mentions(query: str = "") -> CallToolResult:
    """Search scene objects, materials and collections to @-mention in the composer."""
    try:
        items = _scene_items(query)
    except Exception as e:
        logger.debug(f"Mention search failed: {e}")
        items = []
    links = [
        ResourceLink(
            type="resource_link",
            uri=_scene_item_uri(item["kind"], item["name"]),
            name=item["name"],
            title=item["name"],
            description=item.get("detail"),
            mimeType="application/json",
        ).model_dump(by_alias=True, exclude_none=True, mode="json")
        for item in items
        if item.get("kind") in _SCENE_ITEM_KINDS
    ]
    return CallToolResult(content=[], structuredContent={"items": links})


@mcp.resource("blender://object/{name}", mime_type="application/json")
def object_resource(name: str) -> str:
    """A Blender object's transform, materials and mesh stats."""
    return json.dumps(get_blender_connection().send_command("get_object_info", {"name": unquote(name)}))


def _scene_item_resource(kind: str, name: str) -> str:
    name = unquote(name)
    for item in _scene_items(name, limit=100):
        if item.get("kind") == kind and item.get("name") == name:
            return json.dumps(item)
    raise ValueError(f"No {kind} named {name!r} in the open Blender file")


@mcp.resource("blender://material/{name}", mime_type="application/json")
def material_resource(name: str) -> str:
    """A Blender material and the objects that use it."""
    return _scene_item_resource("material", name)


@mcp.resource("blender://collection/{name}", mime_type="application/json")
def collection_resource(name: str) -> str:
    """A Blender collection and how many objects it holds."""
    return _scene_item_resource("collection", name)


@mcp.resource(
    VIEWPORT_URI,
    name="viewport",
    title=VIEWPORT_TITLE,
    mime_type=APP_MIME_TYPE,
    meta={
        "ui": {"prefersBorder": False},
        # Fullscreen only: every screenshot updates the one live view rather
        # than leaving a card in the thread.
        "openai/ui": {"preferredDisplayMode": "fullscreen", "availableDisplayModes": ["fullscreen"]},
    },
)
def viewport_app() -> str:
    return viewport_html()


def _png_content(png: bytes) -> ImageContent:
    return ImageContent(type="image", data=base64.b64encode(png).decode("ascii"), mimeType="image/png")


def _viewport_snapshot() -> tuple[dict, bytes | None]:
    state, png = viewport_store.snapshot()
    # An addon older than this server can't pick objects, so the app says to
    # update it instead of quietly attaching only the image.
    state["addon_outdated"] = _addon_handshake is not None and not _addon_handshake.up_to_date
    return state, png


def _viewport_result(since: int) -> CallToolResult:
    """The viewport state, with the image only when it is newer than `since`."""
    state, png = _viewport_snapshot()
    content = []
    if png is not None and state["seq"] > since:
        content.append(_png_content(png))
    return CallToolResult(content=content, structuredContent=state)


@mcp.tool(
    title=VIEWPORT_TITLE,
    annotations=_READ_ONLY,
    icons=[viewport_icon()],
    meta={
        "ui": {"resourceUri": VIEWPORT_URI, "visibility": ["app"]},
        "openai/ui": {"entrypoints": [{"type": "thread"}]},
    },
)
def open_viewport() -> CallToolResult:
    """Show the latest Blender viewport screenshot beside the conversation."""
    return _viewport_result(since=0)


@mcp.tool(annotations=_READ_ONLY, meta=_APP_ONLY)
def viewport_latest(since: int = 0) -> CallToolResult:
    """The latest viewport screenshot, if newer than `since`. Never touches Blender."""
    return _viewport_result(since)


@mcp.tool(meta=_APP_ONLY)
def viewport_capture(max_size: int = 1000, auto: bool = False) -> CallToolResult:
    """Capture a fresh viewport screenshot for the Viewport app.

    `auto` marks a capture the app took on its own after the scene changed,
    rather than one the user asked for with Refresh.
    """
    try:
        _store_capture(max_size, "auto" if auto else "user")
    except Exception as e:
        return _app_error(f"Couldn't capture the viewport: {e}")
    return _viewport_result(since=0)


def _app_error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


@mcp.tool(annotations=_READ_ONLY, meta=_APP_ONLY)
def viewport_pick(seq: int, x: float, y: float) -> CallToolResult:
    """The object under a click on viewport capture `seq`.

    `x` and `y` run 0..1 from the image's top-left corner. The ray uses the
    camera that capture was rendered with, so it works after the user has
    orbited the view, against the scene as it is now.
    """
    view = viewport_store.view(seq)
    if view is None:
        return _app_error("This screenshot can't be clicked on. Press Refresh for a new one.")
    try:
        hit = get_blender_connection().send_command("pick_viewport_object", {**view, "x": x, "y": y})
    except Exception as e:
        return _app_error(f"Couldn't reach Blender: {e}")
    hit = hit if isinstance(hit, dict) else {}
    if hit.get("mismatch") == "file":
        name = os.path.basename(view.get("file") or "") or "an unsaved file"
        return _app_error(f"This screenshot is of {name}, which isn't open in Blender now. Press Refresh for a new one.")
    if hit.get("mismatch") == "scene":
        return _app_error(
            f"This screenshot is of the scene '{view.get('scene')}', but Blender is showing "
            f"'{hit.get('current')}'. Switch back to it, or press Refresh."
        )
    obj = hit.get("object")
    if not obj:
        return CallToolResult(content=[], structuredContent={"object": None})
    link = ResourceLink(
        type="resource_link",
        uri=_scene_item_uri("object", obj["name"]),
        name=obj["name"],
        title=obj["name"],
        description=obj.get("detail"),
        mimeType="application/json",
    ).model_dump(by_alias=True, exclude_none=True, mode="json")
    return CallToolResult(content=[], structuredContent={"object": {**obj, "link": link}})


_client_features_logged = False


def _log_client_features(session) -> None:
    """Log once what the client advertised, since that decides which UI features it gets."""
    global _client_features_logged
    if _client_features_logged:
        return
    _client_features_logged = True
    params = getattr(session, "client_params", None)
    info = getattr(params, "clientInfo", None)
    logger.info(
        f"MCP client {getattr(info, 'name', '?')} {getattr(info, 'version', '')}: "
        f"extensions={sorted(client_extensions(session))}, apps={supports_apps(session)}, "
        f"openai_forms={supports_openai_forms(session)}"
    )


async def _list_tools_for_client():
    tools = await mcp.list_tools()
    try:
        session = mcp.get_context().session
    except Exception:
        return tools
    _log_client_features(session)
    if supports_apps(session):
        return tools
    return [t for t in tools if not is_app_only(t)]


mcp._mcp_server.list_tools()(_list_tools_for_client)


# MCP Apps and OpenAI extensions. Tools marked visibility ["app"] are called by
# the host UI, never by the model, and are hidden from clients without MCP Apps.

_APP_ONLY = {"ui": {"visibility": ["app"]}}
_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_SCENE_ITEM_KINDS = ("object", "material", "collection")


def _scene_items(query: str, limit: int = 30) -> list[dict]:
    blender = get_blender_connection()
    if (_addon_protocol() or 0) >= 12:
        result = blender.send_command("list_scene_items", {"query": query, "limit": limit})
        return result.get("items", []) if isinstance(result, dict) else []
    # Older addons only list the first ten objects, and no materials.
    result = blender.send_command("get_scene_info")
    needle = query.strip().lower()
    return [
        {"kind": "object", "name": o["name"], "detail": f"{o.get('type', '').title()} object"}
        for o in (result.get("objects") or [])
        if needle in o["name"].lower()
    ]


def _scene_item_uri(kind: str, name: str) -> str:
    return f"blender://{kind}/{quote(name, safe='')}"


@mcp.tool(
    title="Mention Blender items",
    annotations=_READ_ONLY,
    meta={"openai/extensions": {"mentions/search": {}}, **_APP_ONLY},
)
async def search_mentions(query: str = "") -> CallToolResult:
    """Search scene objects, materials and collections to @-mention in the composer."""
    try:
        items = _scene_items(query)
    except Exception as e:
        logger.debug(f"Mention search failed: {e}")
        items = []
    links = [
        ResourceLink(
            type="resource_link",
            uri=_scene_item_uri(item["kind"], item["name"]),
            name=item["name"],
            title=item["name"],
            description=item.get("detail"),
            mimeType="application/json",
        ).model_dump(by_alias=True, exclude_none=True, mode="json")
        for item in items
        if item.get("kind") in _SCENE_ITEM_KINDS
    ]
    return CallToolResult(content=[], structuredContent={"items": links})


@mcp.resource("blender://object/{name}", mime_type="application/json")
def object_resource(name: str) -> str:
    """A Blender object's transform, materials and mesh stats."""
    return json.dumps(get_blender_connection().send_command("get_object_info", {"name": unquote(name)}))


def _scene_item_resource(kind: str, name: str) -> str:
    name = unquote(name)
    for item in _scene_items(name, limit=100):
        if item.get("kind") == kind and item.get("name") == name:
            return json.dumps(item)
    raise ValueError(f"No {kind} named {name!r} in the open Blender file")


@mcp.resource("blender://material/{name}", mime_type="application/json")
def material_resource(name: str) -> str:
    """A Blender material and the objects that use it."""
    return _scene_item_resource("material", name)


@mcp.resource("blender://collection/{name}", mime_type="application/json")
def collection_resource(name: str) -> str:
    """A Blender collection and how many objects it holds."""
    return _scene_item_resource("collection", name)


@mcp.resource(
    VIEWPORT_URI,
    name="viewport",
    title=VIEWPORT_TITLE,
    mime_type=APP_MIME_TYPE,
    meta={
        "ui": {"prefersBorder": False},
        # Fullscreen only: every screenshot updates the one live view rather
        # than leaving a card in the thread.
        "openai/ui": {"preferredDisplayMode": "fullscreen", "availableDisplayModes": ["fullscreen"]},
    },
)
def viewport_app() -> str:
    return viewport_html()


def _png_content(png: bytes) -> ImageContent:
    return ImageContent(type="image", data=base64.b64encode(png).decode("ascii"), mimeType="image/png")


def _viewport_snapshot() -> tuple[dict, bytes | None]:
    state, png = viewport_store.snapshot()
    # An addon older than this server can't pick objects, so the app says to
    # update it instead of quietly attaching only the image.
    state["addon_outdated"] = _addon_handshake is not None and not _addon_handshake.up_to_date
    return state, png


def _viewport_result(since: int) -> CallToolResult:
    """The viewport state, with the image only when it is newer than `since`."""
    state, png = _viewport_snapshot()
    content = []
    if png is not None and state["seq"] > since:
        content.append(_png_content(png))
    return CallToolResult(content=content, structuredContent=state)


@mcp.tool(
    title=VIEWPORT_TITLE,
    annotations=_READ_ONLY,
    icons=[viewport_icon()],
    meta={
        "ui": {"resourceUri": VIEWPORT_URI, "visibility": ["app"]},
        "openai/ui": {"entrypoints": [{"type": "thread"}]},
    },
)
def open_viewport() -> CallToolResult:
    """Show the latest Blender viewport screenshot beside the conversation."""
    return _viewport_result(since=0)


@mcp.tool(annotations=_READ_ONLY, meta=_APP_ONLY)
def viewport_latest(since: int = 0) -> CallToolResult:
    """The latest viewport screenshot, if newer than `since`. Never touches Blender."""
    return _viewport_result(since)


@mcp.tool(meta=_APP_ONLY)
def viewport_capture(max_size: int = 1000, auto: bool = False) -> CallToolResult:
    """Capture a fresh viewport screenshot for the Viewport app.

    `auto` marks a capture the app took on its own after the scene changed,
    rather than one the user asked for with Refresh.
    """
    try:
        _store_capture(max_size, "auto" if auto else "user")
    except Exception as e:
        return _app_error(f"Couldn't capture the viewport: {e}")
    return _viewport_result(since=0)


def _app_error(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], isError=True)


@mcp.tool(annotations=_READ_ONLY, meta=_APP_ONLY)
def viewport_pick(seq: int, x: float, y: float) -> CallToolResult:
    """The object under a click on viewport capture `seq`.

    `x` and `y` run 0..1 from the image's top-left corner. The ray uses the
    camera that capture was rendered with, so it works after the user has
    orbited the view, against the scene as it is now.
    """
    view = viewport_store.view(seq)
    if view is None:
        return _app_error("This screenshot can't be clicked on. Press Refresh for a new one.")
    try:
        hit = get_blender_connection().send_command("pick_viewport_object", {**view, "x": x, "y": y})
    except Exception as e:
        return _app_error(f"Couldn't reach Blender: {e}")
    hit = hit if isinstance(hit, dict) else {}
    if hit.get("mismatch") == "file":
        name = os.path.basename(view.get("file") or "") or "an unsaved file"
        return _app_error(f"This screenshot is of {name}, which isn't open in Blender now. Press Refresh for a new one.")
    if hit.get("mismatch") == "scene":
        return _app_error(
            f"This screenshot is of the scene '{view.get('scene')}', but Blender is showing "
            f"'{hit.get('current')}'. Switch back to it, or press Refresh."
        )
    obj = hit.get("object")
    if not obj:
        return CallToolResult(content=[], structuredContent={"object": None})
    link = ResourceLink(
        type="resource_link",
        uri=_scene_item_uri("object", obj["name"]),
        name=obj["name"],
        title=obj["name"],
        description=obj.get("detail"),
        mimeType="application/json",
    ).model_dump(by_alias=True, exclude_none=True, mode="json")
    return CallToolResult(content=[], structuredContent={"object": {**obj, "link": link}})


_client_features_logged = False


def _log_client_features(session) -> None:
    """Log once what the client advertised, since that decides which UI features it gets."""
    global _client_features_logged
    if _client_features_logged:
        return
    _client_features_logged = True
    params = getattr(session, "client_params", None)
    info = getattr(params, "clientInfo", None)
    logger.info(
        f"MCP client {getattr(info, 'name', '?')} {getattr(info, 'version', '')}: "
        f"extensions={sorted(client_extensions(session))}, apps={supports_apps(session)}, "
        f"openai_forms={supports_openai_forms(session)}"
    )


async def _list_tools_for_client():
    tools = await mcp.list_tools()
    try:
        session = mcp.get_context().session
    except Exception:
        return tools
    _log_client_features(session)
    if supports_apps(session):
        return tools
    return [t for t in tools if not is_app_only(t)]


mcp._mcp_server.list_tools()(_list_tools_for_client)


# Main execution

def main():
    """Run the MCP server, or addon install CLI subcommands."""
    global CLI_HOST, CLI_PORT

<<<<<<< HEAD
    if len(sys.argv) > 1 and sys.argv[1] in {"install-addon", "addon-paths", "setup", "-h", "--help"}:
=======
    if len(sys.argv) > 1 and sys.argv[1] in {"install-addon", "addon-paths", "setup", "update", "-h", "--help"}:
>>>>>>> upstream/main
        code = run_addon_cli(sys.argv[1:])
        if code >= 0:
            raise SystemExit(code)

    CLI_HOST, CLI_PORT = parse_connection_args(sys.argv[1:])

    # When run by hand (stdin is a TTY) the server appears to "hang" while it
    # silently waits for an MCP client; log a hint so that state is obvious.
    # Launched by a client, stdin is a pipe so this is skipped, and logging goes
    # to stderr, never to the stdio protocol on stdout.
    try:
        interactive = sys.stdin.isatty()
    except (AttributeError, OSError):
        interactive = False
    if interactive:
        logger.info(
            "BlenderMCP is an MCP server and is meant to be launched by your MCP "
            "client (Claude Desktop, Cursor, VS Code, ...), not run by hand. "
            "It will now wait silently for a client on stdin -- that is normal, "
            "not a hang. Press Ctrl-C to exit. "
            "Setup guide: https://github.com/ahujasid/blender-mcp#installation "
            "(if the addon is outdated this logs how to update it: uvx mcp-for-blender install-addon)"
        )
    context_log.install(mcp, SERVER_INSTRUCTIONS)
    mcp.run()

if __name__ == "__main__":
    main()