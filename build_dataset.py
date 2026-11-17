"""Build a Power BI ready dataset from StatsBomb open data.

Downloads every match of a competition and season, and writes a small star schema
of CSV files to data/:

    dim_team.csv           one row per team
    dim_player.csv         one row per player (main team, position group)
    dim_match.csv          one row per match
    fact_player_match.csv  one row per player and match (minutes and actions)
    fact_team_match.csv    one row per team and match (goals, xG, points)
    fact_shots.csv         one row per shot (location, xG, outcome) for shot maps
    player_season.csv      season totals, per 90 values and position percentiles
    player_percentiles.csv one row per player and metric (for percentile bar charts)

Example:
    python build_dataset.py                      # LaLiga 2015/16
    python build_dataset.py --competition 2 --season 27   # Premier League 2015/16
"""

import argparse
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from statsbombpy import sb
from tqdm.auto import tqdm

warnings.filterwarnings("ignore")

FINAL_THIRD_X = 80      # StatsBomb pitch is 120 x 80 yards
PROGRESSIVE_YARDS = 10  # a pass or carry that moves the ball at least 10 yards forward
MIN_MINUTES_PERCENTILE = 900


# ---------------------------------------------------------------- helpers

def position_group(name):
    if not isinstance(name, str):
        return "Unknown"
    if "Goalkeeper" in name:
        return "Goalkeeper"
    if "Back" in name:
        return "Defender"
    if "Midfield" in name:
        return "Midfielder"
    return "Forward"


def col(df, name):
    """Column if present, otherwise a column of NaN (open data varies between seasons)."""
    return df[name] if name in df.columns else pd.Series(np.nan, index=df.index)


def xy(series, idx):
    return series.apply(lambda v: v[idx] if isinstance(v, (list, tuple)) and len(v) > idx else np.nan)


# ---------------------------------------------------------------- per match

def minutes_played(ev):
    """Minutes per player from the starting lineups, substitutions and red cards."""
    t = ev["minute"] + ev["second"] / 60
    end = float(t.max())
    on, off, team_of, pos_of, name_of = {}, {}, {}, {}, {}

    for _, row in ev[ev["type"] == "Starting XI"].iterrows():
        for p in row["tactics"]["lineup"]:
            pid = p["player"]["id"]
            on[pid] = 0.0
            team_of[pid] = row["team_id"]
            pos_of[pid] = p["position"]["name"]
            name_of[pid] = p["player"]["name"]

    subs = ev[ev["type"] == "Substitution"]
    for _, row in subs.iterrows():
        when = row["minute"] + row["second"] / 60
        off[row["player_id"]] = when
        new = row.get("substitution_replacement_id")
        if pd.notna(new):
            on[int(new)] = when
            team_of[int(new)] = row["team_id"]
            name_of[int(new)] = row.get("substitution_replacement")
            # fallback position: the position of the player being replaced
            pos_of.setdefault(int(new), None)
            sub_pos = row.get("position")
            if sub_pos is not None and not pd.isna(sub_pos):
                pos_of[int(new)] = pos_of[int(new)] or sub_pos

    reds = pd.Series(False, index=ev.index)
    for c in ("foul_committed_card", "bad_behaviour_card"):
        reds |= col(ev, c).isin(["Red Card", "Second Yellow"])
    for _, row in ev[reds].iterrows():
        off[row["player_id"]] = row["minute"] + row["second"] / 60

    # position for substitutes: the most common position in their events
    played = ev.dropna(subset=["player_id"])
    common = played.groupby("player_id")["position"].agg(lambda s: s.mode().iloc[0] if len(s.mode()) else None)
    rows = []
    for pid, start in on.items():
        stop = off.get(pid, end)
        rows.append({
            "player_id": int(pid),
            "team_id": int(team_of[pid]),
            "minutes": round(max(stop - start, 0.0), 1),
            "started": start == 0.0,
            "position": pos_of.get(pid) or common.get(pid),
            "player_name": name_of.get(pid),
        })
    return pd.DataFrame(rows)


def player_actions(ev):
    """Counts of actions per player in one match."""
    e = ev.dropna(subset=["player_id"]).copy()
    e["player_id"] = e["player_id"].astype(int)
    sx, ex = xy(e["location"], 0), xy(col(e, "pass_end_location"), 0)
    cx = xy(col(e, "carry_end_location"), 0)

    is_pass = e["type"] == "Pass"
    completed = is_pass & col(e, "pass_outcome").isna()
    is_shot = e["type"] == "Shot"
    penalty = col(e, "shot_type") == "Penalty"

    # expected assists: xG of the shot each key pass created
    shots = e[is_shot]
    xa = shots.groupby(col(shots, "shot_key_pass_id"))["shot_statsbomb_xg"].sum()
    e["xa"] = e["id"].map(xa).fillna(0.0)

    f = pd.DataFrame({
        "player_id": e["player_id"],
        "shots": is_shot,
        "goals": is_shot & (col(e, "shot_outcome") == "Goal"),
        "np_goals": is_shot & (col(e, "shot_outcome") == "Goal") & ~penalty,
        "xg": np.where(is_shot, col(e, "shot_statsbomb_xg").fillna(0), 0.0),
        "np_xg": np.where(is_shot & ~penalty, col(e, "shot_statsbomb_xg").fillna(0), 0.0),
        "assists": is_pass & (col(e, "pass_goal_assist") == True),
        "key_passes": is_pass & ((col(e, "pass_shot_assist") == True) | (col(e, "pass_goal_assist") == True)),
        "xa": np.where(is_pass, e["xa"], 0.0),
        "passes": is_pass,
        "passes_completed": completed,
        "progressive_passes": completed & ((ex - sx) >= PROGRESSIVE_YARDS),
        "passes_final_third": completed & (sx < FINAL_THIRD_X) & (ex >= FINAL_THIRD_X),
        "progressive_carries": (e["type"] == "Carry") & ((cx - sx) >= PROGRESSIVE_YARDS),
        "dribbles": e["type"] == "Dribble",
        "dribbles_completed": (e["type"] == "Dribble") & (col(e, "dribble_outcome") == "Complete"),
        "tackles_won": (e["type"] == "Duel") & (col(e, "duel_type") == "Tackle")
                       & col(e, "duel_outcome").isin(["Won", "Success", "Success In Play", "Success Out"]),
        "interceptions": (e["type"] == "Interception")
                         & ~col(e, "interception_outcome").isin(["Lost", "Lost In Play", "Lost Out"]),
        "ball_recoveries": e["type"] == "Ball Recovery",
        "pressures": e["type"] == "Pressure",
        "clearances": e["type"] == "Clearance",
    })
    out = f.groupby("player_id").sum(numeric_only=True)
    for c in out.columns:
        if c not in ("xg", "np_xg", "xa"):
            out[c] = out[c].astype(int)
    return out.reset_index()


def shot_rows(ev, match_id):
    s = ev[ev["type"] == "Shot"].copy()
    if s.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "shot_id": s["id"],
        "match_id": match_id,
        "player_id": s["player_id"].astype(int),
        "team_id": s["team_id"].astype(int),
        "minute": s["minute"],
        "x": xy(s["location"], 0),
        "y": xy(s["location"], 1),
        "y_plot": 80 - xy(s["location"], 1),  # Power BI y axis grows upwards
        "xg": col(s, "shot_statsbomb_xg"),
        "outcome": col(s, "shot_outcome"),
        "is_goal": col(s, "shot_outcome") == "Goal",
        "body_part": col(s, "shot_body_part"),
        "shot_type": col(s, "shot_type"),
    })


# ---------------------------------------------------------------- season tables

def team_match_table(matches, pm, shots):
    rows = []
    for _, m in matches.iterrows():
        for side, other in (("home", "away"), ("away", "home")):
            tid, oid = int(m[f"{side}_team_id"]), int(m[f"{other}_team_id"])
            gf, ga = int(m[f"{side}_score"]), int(m[f"{other}_score"])
            s = shots[shots["match_id"] == m["match_id"]]
            rows.append({
                "match_id": m["match_id"], "team_id": tid, "opponent_id": oid,
                "venue": side.capitalize(), "goals_for": gf, "goals_against": ga,
                "xg_for": s.loc[s.team_id == tid, "xg"].sum(),
                "xg_against": s.loc[s.team_id == oid, "xg"].sum(),
                "shots_for": int((s.team_id == tid).sum()),
                "shots_against": int((s.team_id == oid).sum()),
                "points": 3 if gf > ga else 1 if gf == ga else 0,
                "result": "W" if gf > ga else "D" if gf == ga else "L",
            })
    tm = pd.DataFrame(rows)
    passes = pm.groupby(["match_id", "team_id"])["passes"].sum().rename("passes").reset_index()
    tm = tm.merge(passes, on=["match_id", "team_id"], how="left")
    total = tm.groupby("match_id")["passes"].transform("sum")
    tm["pass_share"] = (tm["passes"] / total).round(4)
    return tm


PER90 = ["goals", "np_goals", "xg", "np_xg", "assists", "xa", "shots", "key_passes",
         "progressive_passes", "passes_final_third", "progressive_carries", "dribbles_completed",
         "tackles_won", "interceptions", "ball_recoveries", "pressures"]


def player_season_table(pm, dim_player):
    tot = pm.groupby("player_id").sum(numeric_only=True).drop(columns=["match_id", "team_id"], errors="ignore")
    tot["matches"] = pm.groupby("player_id")["match_id"].nunique()
    tot["starts"] = pm.groupby("player_id")["started"].sum()
    tot["pass_completion"] = (tot["passes_completed"] / tot["passes"].replace(0, np.nan)).round(4)
    for c in PER90:
        tot[f"{c}_p90"] = (tot[c] * 90 / tot["minutes"].replace(0, np.nan)).round(3)
    tot = tot.reset_index().merge(dim_player[["player_id", "position_group"]], on="player_id", how="left")

    # percentiles within the same position group, among players with enough minutes
    eligible = tot["minutes"] >= MIN_MINUTES_PERCENTILE
    tot["eligible_percentiles"] = eligible
    for c in PER90 + ["pass_completion"]:
        key = c if c == "pass_completion" else f"{c}_p90"
        pct = tot[eligible].groupby("position_group")[key].rank(pct=True)
        tot[f"{key}_pct"] = (pct * 100).round(0)
    return tot


LABELS = {
    "goals_p90": "Goals", "np_goals_p90": "Non penalty goals", "xg_p90": "xG", "np_xg_p90": "Non penalty xG",
    "assists_p90": "Assists", "xa_p90": "xA", "shots_p90": "Shots", "key_passes_p90": "Key passes",
    "progressive_passes_p90": "Progressive passes", "passes_final_third_p90": "Passes into final third",
    "progressive_carries_p90": "Progressive carries", "dribbles_completed_p90": "Successful dribbles",
    "tackles_won_p90": "Tackles won", "interceptions_p90": "Interceptions",
    "ball_recoveries_p90": "Ball recoveries", "pressures_p90": "Pressures", "pass_completion": "Pass completion",
}


def percentile_long(season):
    """One row per player and metric, ready for a bar chart of percentiles in Power BI."""
    rows = []
    elig = season[season["eligible_percentiles"]]
    for key, label in LABELS.items():
        part = elig[["player_id", key, f"{key}_pct"]].rename(columns={key: "value", f"{key}_pct": "percentile"})
        part.insert(1, "metric", label)
        part.insert(2, "metric_order", list(LABELS).index(key) + 1)
        rows.append(part)
    return pd.concat(rows, ignore_index=True)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--competition", type=int, default=11, help="StatsBomb competition id (11 = La Liga)")
    ap.add_argument("--season", type=int, default=27, help="StatsBomb season id (27 = 2015/16)")
    ap.add_argument("--out", default="data")
    ap.add_argument("--max-matches", type=int, default=None, help="only for quick tests")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    matches = sb.matches(competition_id=args.competition, season_id=args.season)
    matches = matches[matches["match_status"] == "available"].sort_values("match_date")
    if args.max_matches:
        matches = matches.head(args.max_matches)

    pm_rows, shot_tables, names, positions = [], [], {}, []
    short, nationality = {}, {}
    for mid in tqdm(matches["match_id"], desc="matches"):
        ev = sb.events(match_id=int(mid))
        mins = minutes_played(ev)
        acts = player_actions(ev)
        pm = mins.drop(columns=["player_name"]).merge(acts, on="player_id", how="left").fillna(0)
        pm.insert(0, "match_id", int(mid))
        pm_rows.append(pm)
        shot_tables.append(shot_rows(ev, int(mid)))
        named = ev.dropna(subset=["player_id"])
        names.update(dict(zip(named["player_id"].astype(int), named["player"])))
        names.update({int(k): v for k, v in zip(mins["player_id"], mins["player_name"]) if isinstance(v, str)})
        for lineup in sb.lineups(match_id=int(mid)).values():
            for _, p in lineup.iterrows():
                pid = int(p["player_id"])
                short[pid] = p["player_nickname"] if isinstance(p.get("player_nickname"), str) else p["player_name"]
                if isinstance(p.get("country"), str):
                    nationality[pid] = p["country"]
        positions.append(pm[["player_id", "position", "minutes"]])

    pm = pd.concat(pm_rows, ignore_index=True)
    int_cols = [c for c in pm.columns if c not in ("xg", "np_xg", "xa", "minutes", "position", "started")]
    pm[int_cols] = pm[int_cols].astype(int)
    shots = pd.concat(shot_tables, ignore_index=True)

    # dimensions
    teams = pd.concat([
        matches[["home_team_id", "home_team"]].set_axis(["team_id", "team_name"], axis=1),
        matches[["away_team_id", "away_team"]].set_axis(["team_id", "team_name"], axis=1),
    ]).drop_duplicates("team_id").sort_values("team_name")

    pos = pd.concat(positions)
    pos["position_group"] = pos["position"].map(position_group)
    main_pos = (pos.groupby(["player_id", "position_group"])["minutes"].sum().reset_index()
                .sort_values("minutes").drop_duplicates("player_id", keep="last"))
    main_team = (pm.groupby(["player_id", "team_id"])["minutes"].sum().reset_index()
                 .sort_values("minutes").drop_duplicates("player_id", keep="last"))
    dim_player = (main_pos[["player_id", "position_group"]]
                  .merge(main_team[["player_id", "team_id"]], on="player_id")
                  .merge(teams, on="team_id", how="left"))
    dim_player.insert(1, "player_name", dim_player["player_id"].map(short).fillna(dim_player["player_id"].map(names)))
    dim_player.insert(2, "full_name", dim_player["player_id"].map(names))
    dim_player.insert(3, "nationality", dim_player["player_id"].map(nationality))
    dim_player = dim_player.rename(columns={"team_id": "main_team_id", "team_name": "main_team"})

    dim_match = pd.DataFrame({
        "match_id": matches["match_id"],
        "date": pd.to_datetime(matches["match_date"]).dt.date,
        "matchweek": matches["match_week"],
        "home_team_id": matches["home_team_id"], "away_team_id": matches["away_team_id"],
        "home_team": matches["home_team"], "away_team": matches["away_team"],
        "home_score": matches["home_score"], "away_score": matches["away_score"],
    })
    dim_match["label"] = (dim_match["home_team"] + " " + dim_match["home_score"].astype(str) + "-"
                          + dim_match["away_score"].astype(str) + " " + dim_match["away_team"])

    team_match = team_match_table(matches, pm, shots)
    season = player_season_table(pm, dim_player)

    teams.to_csv(out / "dim_team.csv", index=False)
    dim_player.to_csv(out / "dim_player.csv", index=False)
    dim_match.to_csv(out / "dim_match.csv", index=False)
    pm.drop(columns=["position"]).to_csv(out / "fact_player_match.csv", index=False)
    team_match.to_csv(out / "fact_team_match.csv", index=False)
    shots.to_csv(out / "fact_shots.csv", index=False)
    season.to_csv(out / "player_season.csv", index=False)
    percentile_long(season).to_csv(out / "player_percentiles.csv", index=False)
    print(f"{len(dim_match)} matches, {len(dim_player)} players, {len(shots)} shots written to {out}/")


if __name__ == "__main__":
    main()
