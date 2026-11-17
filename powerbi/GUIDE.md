# Building the dashboard in Power BI Desktop

The finished report is `laliga_2015_16_dashboard.pbix`. This guide explains how it was
built, so it can be reproduced or adapted to another league or season.

## 1. Load the data

1. **Home > Get data > Text/CSV** and load every file in `data/` into the same report:
   `dim_team`, `dim_player`, `dim_match`, `fact_player_match`, `fact_team_match`,
   `fact_shots`, `player_season`, `player_percentiles`.
2. Check decimals: `minutes` in `fact_player_match` should read like 93.1, not 931. If it
   does not, set **File > Options > Current file > Regional settings > Locale for import**
   to English (United States) and refresh.

## 2. Model

Relationships (one to many, single direction), checked in **Manage relationships**:

| From | To |
|---|---|
| `fact_player_match[match_id]` | `dim_match[match_id]` |
| `fact_player_match[player_id]` | `dim_player[player_id]` |
| `fact_player_match[team_id]` | `dim_team[team_id]` |
| `fact_shots[match_id]` | `dim_match[match_id]` |
| `fact_shots[player_id]` | `dim_player[player_id]` |
| `fact_shots[team_id]` | `dim_team[team_id]` |
| `fact_team_match[match_id]` | `dim_match[match_id]` |
| `fact_team_match[team_id]` | `dim_team[team_id]` |
| `player_percentiles[player_id]` | `dim_player[player_id]` |
| `player_season[player_id]` | `dim_player[player_id]` (one to one) |

## 3. Measures and parameters

1. **Home > Enter data**, name the table `metrics` and load it (hide `Column1`).
2. **Modeling > New parameter > Numeric range**: `Min Minutes`, whole number, 0 to 3000,
   increment 90, default 900.
3. Open the **DAX query view**, paste `measures_dax_query.txt`, run it and click
   **Update model with changes** to create the 53 core measures at once.
4. **Modeling > New parameter > Fields**: keep the default name `Parameter` and add, in
   this order, `xG p90`, `Non Penalty Goals p90`, `xA p90`, `Key Passes p90`,
   `Progressive Passes p90`, `Progressive Carries p90`, `Successful Dribbles p90`,
   `Tackles and Interceptions p90`, `Pressures p90`.
5. Add the helper measures at the end of `measures.dax` (`Selected Metric Value`,
   `Top 10 Title`, the three `Label` measures and `Shot Colour`) with **New measure**.
6. In the Table view, sort `player_percentiles[metric]` by `metric_order`
   (**Column tools > Sort by column**).

## 4. Pages

### League overview
- Table: team, matches, wins, draws, losses, goals for and against, goal difference,
  points (data bars) and xG for and against, sorted by points.
- Scatter chart of `xG For` against `xG Against` with category labels, inverted Y axis
  (better teams at the top right) and average lines from the Analytics pane.
- Cards with `Goals Scored Label`, `Total xG Label` and `Conversion Rate` (percentage).
  The new card visual shows 1K instead of 1,043, which is why the label measures exist.

### Player ranking
- Slicers: team and position (dropdown), `Min Minutes` (slider) and `Parameter`
  (dropdown, single select, header renamed to Metric).
- Table of players with per 90 metrics and data bars, filtered with `Eligible Player`
  is 1, so the minutes slider controls who appears.
- Top 10 bar chart: Y axis `player_name`, X axis `Selected Metric Value`, Top N filter
  on `player_name` (top 10 by `Selected Metric Value`), sorted descending, title from
  `Top 10 Title` (**Title > fx > Field value**). A Top N filter by the field parameter
  itself does not work, which is why `Selected Metric Value` exists.

### Player profile
- Player slicer (dropdown, single select, with search).
- One new card visual with team, position, `Minutes Label`, goals, assists, xG and xA.
- Percentile bar chart: Y axis `player_percentiles[metric]`, X axis `percentile`, axis
  fixed from 0 to 100, gradient colours from red (0) through yellow (50) to green (100).
- Shot map: scatter chart with Values `shot_id`, X `x` and Y `y_plot` (both
  *Don't summarize*), Size `xg`, axes fixed to 60 to 120 and 0 to 80 and hidden,
  `pitch_half.png` as **plot area background** with image fit **Fill**. With
  **Fit** the image keeps its proportions and gets cropped, so the shots no longer match
  the pitch; **Fill** stretches it to the plot area and keeps them aligned at any size.
  Check: penalties must sit exactly on the penalty spot.

### Season race
- Team slicer (list, multiple selection) with Barcelona, Real Madrid and Atlético Madrid
  selected by default.
- Line chart: X axis `dim_match[matchweek]`, Y axis `Cumulative Points`, legend
  `team_name`, with markers. The three lines finish on 91, 90 and 88 points.

Every page has a footer: "Data: StatsBomb Open Data".
