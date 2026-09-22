"""
Configuration for Blender MCP telemetry — local fork placeholder values.

This file IS tracked in this fork (removed from .gitignore), so it must never
contain real credentials. Upstream keeps it out of version control because the
shipped copy points at a live Supabase project; every endpoint below is an
inert placeholder instead.

Telemetry is switched off here on purpose, for two reasons:
  1. nothing is ever sent anywhere from this checkout, and
  2. no tool call pays for a network attempt (and a 1.5s timeout) against a
     host that does not exist.

To run telemetry locally, point supabase_url / supabase_anon_key at a project
you control and set enabled = True.
"""
from dataclasses import dataclass


@dataclass
class TelemetryConfig:
    """Telemetry configuration settings"""

    # Placeholder only: `.invalid` is reserved by RFC 2606 and never resolves.
    supabase_url: str = "https://placeholder-project.supabase.invalid"
    supabase_anon_key: str = "placeholder-anon-key-not-a-real-credential"
    enabled: bool = False
    timeout: float = 1.5
    max_prompt_length: int = 1000
    screenshot_max_size: int = 800
    supabase_bucket: str = "telemetry-screenshots"
    trajectory_steps_table: str = "trajectory_steps"
    trajectory_feedback_table: str = "trajectory_feedback"

    def __post_init__(self):
        if not self.supabase_url or not self.supabase_anon_key:
            self.enabled = False


telemetry_config = TelemetryConfig()
