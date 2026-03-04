import json
import logging
import re
from typing import Any, Dict, List, Optional

import yaml

from .utils import dist_time_to_ms, get_pace_range, ms_to_pace, pace_to_ms
from .workout import Target, Workout, WorkoutStep
from .garmin_client import GarminClient


CLEAN_KEYS = ["author", "createdDate", "ownerId", "shared", "updatedDate"]


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


def cmd_import_workouts(args):
    logging.info("importing workouts from " + args.workouts_file)

    client = GarminClient(args.oauth_folder)
    existing_workouts: List[Dict[str, Any]] = []

    if not args.dry_run and args.replace:
        existing_workouts = client.list_workouts()

    for workout in import_workouts(args.workouts_file, args.name_filter):
        # Optional conversion for treadmill workouts
        if args.treadmill or workout.workout_name.strip().endswith("(T)"):
            workout.dist_to_time()

        if args.dry_run:
            print(json.dumps(workout.garminconnect_json()))
            continue

        logging.info("creating workout: " + workout.workout_name)

        workout_id_to_replace: Optional[str] = None
        if args.replace and existing_workouts:
            for ew in existing_workouts:
                if ew.get("workoutName") == workout.workout_name:
                    workout_id_to_replace = str(ew.get("workoutId"))
                    break

        if workout_id_to_replace:
            client.update_workout(workout_id_to_replace, workout)
        else:
            client.add_workout(workout)


def cmd_export_workouts(args):
    """Export workouts currently in Garmin Connect.

    This is intentionally simple but compatible with the original CLI:
    - Supports optional name filter (regex)
    - Optionally cleans some noisy fields
    - Outputs JSON or YAML, either to stdout or to a file
    """

    client = GarminClient(args.oauth_folder)
    logging.info("getting list of workouts.")
    workouts = client.list_workouts()

    # Filter by name
    if args.name_filter:
        pattern = re.compile(args.name_filter)
        workouts = [w for w in workouts if pattern.search(w.get("workoutName", ""))]

    # Clean unwanted keys
    if args.clean:
        for w in workouts:
            for ck in CLEAN_KEYS:
                w.pop(ck, None)

    # Decide output format
    output_format = args.format
    if output_format is None:
        if args.export_file and args.export_file.lower().endswith((".yml", ".yaml")):
            output_format = "YAML"
        else:
            output_format = "JSON"

    if args.export_file:
        fh = open(args.export_file, "w", encoding="utf-8")
    else:
        fh = None

    try:
        if output_format.upper() == "YAML":
            text = yaml.safe_dump(workouts, sort_keys=False, allow_unicode=True)
        else:
            text = json.dumps(workouts, indent=2, ensure_ascii=False)

        if fh is not None:
            fh.write(text)
        else:
            print(text)
    finally:
        if fh is not None:
            fh.close()


def cmd_delete_workouts(args):
    """Delete workouts by explicit id list or name filter."""

    client = GarminClient(args.oauth_folder)

    ids: List[str] = []
    if args.workout_ids:
        ids = [wid.strip() for wid in args.workout_ids.split(",") if wid.strip()]
    else:
        logging.info("getting list of workouts.")
        workouts_list = client.list_workouts()
        pattern = re.compile(args.name_filter) if args.name_filter else None
        for workout in workouts_list:
            name = workout.get("workoutName", "")
            if pattern is None or pattern.search(name):
                ids.append(str(workout.get("workoutId")))

    for wid in ids:
        logging.info(f"deleting workout {wid}")
        if not args.dry_run:
            client.delete_workout(wid)


# ---------------------------------------------------------------------------
# YAML -> Workout conversion
# ---------------------------------------------------------------------------


def import_workouts(plan_file: str, name_filter: Optional[str] = None) -> List[Workout]:
    """Parse a YAML plan file and build Workout objects.

    Supports:
    - Nested repeat blocks
    - "repeat N*" syntax to indicate skip-last-rest behaviour (requires
      corresponding handling in WorkoutStep if used)
    - Time formats like "90s", "2min", "1h", and "mm:ss"
    """

    with open(plan_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("Workout plan YAML must be a mapping at top level")

    config = data.pop("config", {}) or {}
    expand_config(config)

    workouts: List[Workout] = []

    for workout_name, steps in data.items():
        if name_filter and not re.search(name_filter, workout_name):
            continue

        if not isinstance(steps, list):
            raise ValueError(f"Workout '{workout_name}' must be a list of steps")

        full_name = f"{config.get('name_prefix', '')}{workout_name}"
        sport = config.get("sport", "running")
        description = config.get("description")
        w = Workout(sport, full_name, description=description)

        def process_step(step: Dict[str, Any], parent: Optional[WorkoutStep] = None):
            if not isinstance(step, dict) or len(step) != 1:
                raise ValueError(f"Invalid step in workout '{workout_name}': {step}")

            (k, v), = step.items()
            key = str(k).strip()

            # Repeat handling: "repeat N" and "repeat N*" (skip last rest)
            m_star = re.match(r"^repeat\s+(\d+)\*$", key)
            m_plain = re.match(r"^repeat\s+(\d+)$", key)
            if m_star or m_plain:
                iterations = int((m_star or m_plain).group(1))
                skip_last_rest = bool(m_star)

                repeat_step = WorkoutStep(
                    0,
                    "repeat",
                    end_condition="iterations",
                    end_condition_value=str(iterations),
                )
                # Mark skip-last-rest if WorkoutStep supports it
                if hasattr(repeat_step, "skip_last_rest"):
                    repeat_step.skip_last_rest = skip_last_rest  # type: ignore[attr-defined]

                if not isinstance(v, list):
                    raise ValueError(
                        f"Repeat step in workout '{workout_name}' must have a list of substeps"
                    )

                for sub in v:
                    process_step(sub, parent=repeat_step)

                if parent is not None:
                    parent.add_step(repeat_step)
                else:
                    w.add_step(repeat_step)
                return

            # Normal step (interval, rest, warmup, cooldown, recovery, other)
            step_type = key.lower()
            if step_type not in [
                "warmup",
                "cooldown",
                "interval",
                "recovery",
                "rest",
                "other",
            ]:
                step_type = "other"

            if not isinstance(v, str):
                raise ValueError(f"Step '{key}' in workout '{workout_name}' must be a string")

            target = get_target(v, config)
            end_condition = get_end_condition(v)
            end_condition_value = get_end_condition_value(v, end_condition)
            description_step = get_description(v, target)

            ws = WorkoutStep(
                0,
                step_type,
                description_step or "",
                end_condition=end_condition,
                end_condition_value=end_condition_value,
                target=target,
            )

            if parent is not None:
                parent.add_step(ws)
            else:
                w.add_step(ws)

        for s in steps:
            process_step(s)

        workouts.append(w)

    return workouts


# ---------------------------------------------------------------------------
# Helpers for parsing individual steps
# ---------------------------------------------------------------------------


def get_description(step_txt: str, target: Optional[Target] = None) -> Optional[str]:
    description: Optional[str] = None
    if " -- " in step_txt:
        description = step_txt[step_txt.find(" -- ") + 4 :].strip()

    if target and target.target == "pace.zone" and target.from_value and target.to_value:
        avg_pace = (target.from_value + target.to_value) / 2
        avg_pace_kmph = avg_pace / 0.27778
        avg_pace_kmph_str = f"{avg_pace_kmph:.1f} kmph"
        if description:
            description += "\n" + avg_pace_kmph_str
        else:
            description = avg_pace_kmph_str

    return description


def get_end_condition(step_txt: str) -> str:
    step_txt = clean_step(step_txt)

    p_distance = re.compile(r"^\d+(m|km)\s?")
    p_time = re.compile(r"^\d+(min|h|s)\s?")
    p_mmss = re.compile(r"^\d{1,2}:\d{2}$")
    p_iterations = re.compile(r"^\d+$")

    if p_time.match(step_txt) or p_mmss.match(step_txt):
        return "time"
    if p_distance.match(step_txt):
        return "distance"
    if p_iterations.match(step_txt):
        return "iterations"
    return "lap.button"


def get_end_condition_value(step_txt: str, condition_type: Optional[str] = None) -> Optional[str]:
    step_txt = clean_step(step_txt)

    if not condition_type:
        condition_type = get_end_condition(step_txt)

    if condition_type == "time":
        # Formats like 90s, 2min, 1h
        p = re.compile(r"^(\d+)((min|h|s))\s?")
        m = p.match(step_txt)
        if m:
            cv = int(m.group(1))
            tu = m.group(2)
            if tu == "h":
                cv *= 60 * 60
            elif tu == "min":
                cv *= 60
            # "s" = seconds, unchanged
            return str(cv)

        # Format mm:ss
        p_mmss = re.compile(r"^(\d{1,2}):(\d{2})$")
        m2 = p_mmss.match(step_txt)
        if m2:
            minutes = int(m2.group(1))
            seconds = int(m2.group(2))
            total = minutes * 60 + seconds
            return str(total)

        raise ValueError(f"Invalid time format for step: {step_txt}")

    if condition_type == "distance":
        p = re.compile(r"^(\d+)((m|km))\s?")
        m = p.match(step_txt)
        if not m:
            raise ValueError(f"Invalid distance format for step: {step_txt}")
        cv = int(m.group(1))
        tu = m.group(2)
        if tu == "km":
            cv *= 1000
        return str(cv)

    # iterations and lap.button do not need a numeric value here
    return None


def get_target(step_txt: str, config: Dict[str, Any], verbose: bool = False) -> Optional[Target]:
    step_txt = clean_step(step_txt)

    target_type: Optional[str] = None
    target: Optional[str] = None
    scale_min = 1.0
    scale_max = 1.0

    if " in " in step_txt:
        # e.g. "1000m in 3:45"
        target_type = "pace.zone"
        target_ms = dist_time_to_ms(step_txt)
        mmss = ms_to_pace(target_ms)
        target = mmss

    elif " @ " in step_txt:
        # e.g. "1000m @ pace10k" or "1000m @ 3:45-3:35"
        target_type = "pace.zone"
        parts = [p.strip() for p in step_txt.split(" @ ")]
        target = parts[1]

        if not re.compile(r"^\d{1,2}:\d{1,2}(?:-\d{1,2}:\d{1,2})?").match(target):
            # Resolve references like "75% marathon_pace" using config["paces"]
            while not re.compile(r"^\d{1,2}:\d{1,2}(?:-\d{1,2}:\d{1,2})?").match(target):
                tm = re.compile(r"^(\d+-?\d+)%\s*(\S+)$").match(target)
                if tm:
                    scales = sorted(float(s) / 100 for s in tm.group(1).split("-"))
                    scale_min = scale_max = scales[0]
                    if len(scales) == 2:
                        scale_max = scales[1]
                    target = tm.group(2).strip()

                paces = config.get("paces", {}) or {}
                if target in paces:
                    target = paces[target]
                else:
                    raise ValueError(
                        f"Cannot find pace target '{target}' in workout step '{step_txt}'"
                    )

    elif " @hr " in step_txt:
        # Heart-rate zone targets
        target_type = "heart.rate.zone"
        parts = [p.strip() for p in step_txt.split(" @hr ")]
        target = parts[1]
        heart_rates = config.get("heart_rates", {}) or {}
        if target in heart_rates:
            target_val = heart_rates[target]
        else:
            target_val = target

        if isinstance(target_val, int):
            target_val = f"{target_val}-{target_val}"
        target = str(target_val)

    else:
        return None

    if target_type == "pace.zone" and target is not None:
        target_range = get_pace_range(target, config.get("margins"))
        return Target(
            target_type,
            to_value=pace_to_ms(target_range[0]) * scale_min,
            from_value=pace_to_ms(target_range[1]) * scale_max,
        )

    if target_type == "heart.rate.zone" and target is not None:
        if re.compile(r"^\d{2,3}-\d{2,3}$").match(target):
            lo, hi = [int(t) for t in target.split("-")]
            return Target(target_type, lo, hi)
        m = re.compile(r"^(z|zone)[-_]?([1-5])$").match(target)
        if m:
            return Target(target_type, zone=int(m.group(2)))
        raise ValueError("Invalid heart rate target: " + step_txt)

    return None


def clean_step(step_txt: str) -> str:
    """Remove inline description (" -- ") from a step string."""

    if " -- " in step_txt:
        step_txt = step_txt[: step_txt.find(" -- ")].strip()
    return step_txt


def expand_config(config: Dict[str, Any]) -> None:
    """Normalize pace and heart-rate config entries.

    - Paces in "<dist> in <time>" format are converted to mm:ss
    - Heart rate entries expressed as percentages of a reference are expanded
    """

    paces = config.get("paces", {}) or {}
    for pk, pv in list(paces.items()):
        if isinstance(pv, str) and re.compile(r"^.+ in .+$").match(pv.strip()):
            paces[pk] = ms_to_pace(dist_time_to_ms(pv))

    heart_rates = config.get("heart_rates", {}) or {}
    for hrk, hrv in list(heart_rates.items()):
        # If we get an integer, this is a fixed hr. We leave it as it is.
        if isinstance(hrv, int):
            continue

        m = re.compile(r"^\s*(\d{2}-?\d{0,2})% (.+)\s*$").match(str(hrv))
        if not m:
            continue

        ref_hr_name = m.group(2)
        if ref_hr_name not in heart_rates:
            raise ValueError(
                f"Cannot find heart rate target '{ref_hr_name}' in heart rate config. Found in '{hrk}')"
            )

        ref_hr = heart_rates[ref_hr_name]
        hr_range = m.group(1).split("-")

        hr_up = heart_rates.get("hr_up", 0)
        hr_down = heart_rates.get("hr_down", 0)

        # If only one value was given, we apply the margins
        if len(hr_range) == 1:
            hr_range.append(hr_range[0])
            hr_range[0] = str(int(hr_range[0]) - hr_down)
            hr_range[1] = str(int(hr_range[1]) + hr_up)

        low = round(ref_hr * float(hr_range[0]) / 100)
        high = round(ref_hr * float(hr_range[1]) / 100)
        heart_rates[hrk] = f"{low}-{high}"

    # config mutated in place; nothing to return
    return
