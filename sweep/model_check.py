#!/usr/bin/env python3
"""Load sweep/schema.sql into SQLite, prove it against a B580-shaped fixture, render DATA-MODEL.md.

    python3 sweep/model_check.py            load, tag-check, fixture, run every x_ view and script
                                              check; exit 1 on any violation
    python3 sweep/model_check.py --render   regenerate the GENERATED regions of DATA-MODEL.md
    python3 sweep/model_check.py --check    exit 1 if those regions are stale
    python3 sweep/model_check.py --mutate   apply each negative case and prove the named check fires

The schema is the source. Nothing here reads the doc's schema blocks as truth; it writes them.

REFUSES: a schema SQLite cannot load · a table without all three tags · a FILE table whose writer
is not a person (`authored`, `ship` or `viewing`) · a table with no writer or two · a check that never fires on its negative case
· a stale rendered region under --check. Tests: python3 -m unittest tests.test_model_check
"""
import argparse
import re
import sqlite3
import sys
from collections import OrderedDict
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCHEMA = HERE / "schema.sql"
DOC = ROOT / "docs" / "DATA-MODEL.md"
GROUPS = ["reference", "sample", "measurement", "decision"]
DECISION_WRITERS = {"authored", "ship", "viewing"}   # a FILE table is written by a person: directly, or through ship or the viewing
MIN_SQLITE = (3, 37, 0)          # STRICT tables

BEGIN = "<!-- BEGIN GENERATED: {} -->"
END = "<!-- END GENERATED: {} -->"


# ---------------------------------------------------------------- load

def load_schema():
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise SystemExit(f"REFUSING: sqlite {sqlite3.sqlite_version} < {'.'.join(map(str, MIN_SQLITE))}; "
                         "STRICT tables need 3.37")
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text())
    return conn


def parse_tags(sql=None):
    """{table: {group, class, writer, order}} and {x_view: {check, fix}}, from the comment tags.

    A `-- @check` line says what the view refuses; the `-- @fix` line beside it says what to do about
    it. The pair is consumed by the next `CREATE VIEW x_`; a view without them gets empty strings.
    """
    sql = sql if sql is not None else SCHEMA.read_text()
    tables, checks = OrderedDict(), OrderedDict()
    pending = {}
    check_pair = {}
    for line in sql.splitlines():
        m = re.match(r"^--\s*@(group|class|writer)\s+(.+?)\s*$", line)
        if m:
            pending[m.group(1)] = m.group(2)
            continue
        m = re.match(r"^--\s*@(check|fix)\s+(.+?)\s*$", line)
        if m:
            check_pair[m.group(1)] = m.group(2)
            continue
        m = re.match(r"^CREATE TABLE (\w+)", line)
        if m:
            tables[m.group(1)] = dict(pending, order=len(tables))
            pending = {}
            continue
        m = re.match(r"^CREATE VIEW (x_\w+)", line)
        if m:
            checks[m.group(1)] = {"check": check_pair.get("check", ""), "fix": check_pair.get("fix", "")}
            check_pair = {}
    return tables, checks


def db_tables(conn):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]


def db_views(conn, prefix):
    return [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='view' AND name LIKE ? ORDER BY rowid", (prefix + "%",))]


def columns(conn, table):
    return [dict(cid=r[0], name=r[1], type=r[2], notnull=r[3], pk=r[5])
            for r in conn.execute(f"PRAGMA table_info({table})")]


def foreign_keys(conn, table):
    """[(from_cols, to_table, to_cols)] with composite keys grouped."""
    out = OrderedDict()
    for r in conn.execute(f"PRAGMA foreign_key_list({table})"):
        fk_id, seq, to_table, from_col, to_col = r[0], r[1], r[2], r[3], r[4]
        if to_col is None:      # an implicit REFERENCES parent: the parent's primary key, by position
            pk = [c["name"] for c in sorted(columns(conn, to_table), key=lambda c: c["pk"]) if c["pk"]]
            to_col = pk[seq]
        out.setdefault(fk_id, [[], to_table, []])
        out[fk_id][0].append(from_col)
        out[fk_id][2].append(to_col)
    return [(f, t, c) for f, t, c in out.values()]


def enums(conn, table):
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
    out = {}
    for col, vals in re.findall(r"CHECK \((\w+) IN \(([^)]*)\)\)", sql):
        vs = [v.strip().strip("'") for v in vals.split(",")]
        if all(re.match(r"^-?\d+$", v) for v in vs):
            continue                      # 0/1 flags: a bool, not an enumeration worth listing
        out[col] = vs
    return out


# ---------------------------------------------------------------- tag checks

def check_tags(conn, tags):
    problems = []
    present = db_tables(conn)
    for t in present:
        tg = tags.get(t)
        if not tg or any(k not in tg for k in ("group", "class", "writer")):
            problems.append(f"{t}: missing @group/@class/@writer")
            continue
        if tg["group"] not in GROUPS:
            problems.append(f"{t}: unknown @group {tg['group']}")
        if tg["class"] not in ("FILE", "ROW"):
            problems.append(f"{t}: unknown @class {tg['class']}")
        if (tg["class"] == "FILE") != (tg["writer"] in DECISION_WRITERS):
            problems.append(f"{t}: @class {tg['class']} but @writer {tg['writer']} -- FILE means a human wrote it, "
                            f"directly ({'/'.join(sorted(DECISION_WRITERS))}); ROW means a measuring stage did")
    for t in tags:
        if t not in present:
            problems.append(f"{t}: tagged but not created")
    return problems


# ---------------------------------------------------------------- fixture: the B580 lane's shape

FIXTURE = r"""
-- a host is a runtime; media-01 and eta each hold two, nas-01 (the hub's own) has no unit
INSERT INTO host (host, machine, ssh_host, os, work_root, share_root, local_view, ffmpeg, notes, blocked) VALUES
 ('nas-01','nas-01','nas-01','linux','/srv/sweep','/srv/sweep',NULL,NULL,'the hub; the share is local',NULL),
 ('media-01','media-01','media-01','linux','/mnt/data/sweep','/mnt/nas-01/sweep',NULL,'/opt/jellyfin-ffmpeg/bin/ffmpeg',NULL,NULL),
 ('media-01-score','media-01','media-01','linux','/mnt/data/sweep-score','/mnt/nas-01/sweep','/mnt/data/sweep',NULL,'the score container; sees the encode container''s work root at the same path',NULL),
 ('htpc-01','htpc-01','htpc-01','linux','/run/media/system/data/sweep','/mnt/nas-01/sweep',NULL,'/ffmpeg/ffmpeg','root podman; the bind mount is the patched build',
  'mount the sweep tree into tdarr-node, point the work root at it, use /ffmpeg/ffmpeg, then clear this'),
 ('eta','eta','eta','windows','D:\sweep','\\nas-01\sweep',NULL,'c:\Program Files\jellyfin-ffmpeg\bin\ffmpeg.exe','native Windows, no bash',NULL),
 ('eta-wsl','eta','eta','linux','/home/sweep/work','/mnt/nas-01/sweep','/mnt/d',NULL,'the score container under WSL; eta''s own cells are scored through /mnt/d',NULL);

INSERT INTO encoder_unit VALUES
 ('intel-b580-ihd26.2.2-qsv-av1','intel','Arc B580','iHD 26.2.2','qsv','av1'),
 ('nvidia-a4000-595-nvenc-hevc','nvidia','RTX A4000','595.71.05','nvenc','hevc'),
 ('nvidia-5060ti-595-nvenc-av1','nvidia','RTX 5060 Ti','595.71.05','nvenc','av1');

INSERT INTO canonical_concept VALUES
 ('quality_anchor','a position on the codec ladder: -qp, -cq, -global_quality, -q:v'),
 ('rate_control_mode','constant-quantiser versus rate-targeted: -rc constqp, -rc_mode CQP, qsv -q:v'),
 ('preset','the speed/quality ordinal: nvenc p1..p7, qsv 1..7, vaapi -compression_level'),
 ('b_pyramid','B-frame reference structure'),
 ('tiling','AV1 tile columns and rows');

INSERT INTO setting (setting_id, flag, frontend, kind, subsystem, value_type, range_lo, range_hi, is_generic, notes) VALUES
 ('qsv.preset','-preset','qsv','ordinal','other','enum',NULL,NULL,0,'1 is BEST_QUALITY, 7 is fastest'),
 ('qsv.q','-q:v','qsv','quality_anchor','rate_control','int',1,51,0,'selects CQP AND is the anchor'),
 ('qsv.b_strategy','-b_strategy','qsv','option','frame_types','enum',NULL,NULL,0,'default -1 is byte-identical to 1'),
 ('qsv.adaptive_b','-adaptive_b','qsv','option','frame_types','enum',NULL,NULL,0,NULL),
 ('qsv.tile_cols','-tile_cols','qsv','option','tiles','int',1,64,0,NULL),
 ('qsv.tile_rows','-tile_rows','qsv','option','tiles','int',1,64,0,NULL),
 ('qsv.b_v','-b:v','qsv','option','rate_control','int',0,NULL,0,'a rate request; computed from the ceiling'),
 ('nvenc.qp','-qp','nvenc','quality_anchor','rate_control','int',0,51,0,NULL),
 ('nvenc.cq','-cq','nvenc','quality_anchor','rate_control','int',0,51,0,NULL),
 ('nvenc.b_v','-b:v','nvenc','option','rate_control','int',0,NULL,0,'a rate request; computed from the ceiling'),
 ('nvenc.rc','-rc','nvenc','mode_selector','rate_control','enum',NULL,NULL,0,NULL),
 ('nvenc.preset','-preset','nvenc','ordinal','other','enum',NULL,NULL,0,NULL),
 ('nvenc.tune','-tune','nvenc','option','other','enum',NULL,NULL,0,'uhq needs the nv-codec-headers pin'),
 ('generic.compression_level','-compression_level',NULL,'option','plumbing','int',0,12,1,'absent from every private dump; a size lever on AMD, a speed lever on the B580');

INSERT INTO setting_enum_value VALUES
 ('qsv.preset','1'),('qsv.preset','2'),('qsv.preset','3'),('qsv.preset','4'),('qsv.preset','5'),('qsv.preset','6'),('qsv.preset','7'),
 ('qsv.b_strategy','-1'),('qsv.b_strategy','0'),('qsv.b_strategy','1'),
 ('qsv.adaptive_b','-1'),('qsv.adaptive_b','0'),('qsv.adaptive_b','1'),
 ('nvenc.rc','constqp'),('nvenc.rc','vbr'),('nvenc.rc','cbr'),
 ('nvenc.preset','p1'),('nvenc.preset','p2'),('nvenc.preset','p3'),('nvenc.preset','p4'),('nvenc.preset','p5'),('nvenc.preset','p6'),('nvenc.preset','p7'),
 ('nvenc.tune','hq'),('nvenc.tune','uhq'),('nvenc.tune','ll');

INSERT INTO setting_role VALUES
 ('qsv.q','quality_anchor'),('qsv.q','rate_control_mode'),
 ('nvenc.qp','quality_anchor'),('nvenc.cq','quality_anchor'),('nvenc.rc','rate_control_mode'),
 ('qsv.preset','preset'),('nvenc.preset','preset'),('generic.compression_level','preset'),
 ('qsv.b_strategy','b_pyramid'),('qsv.tile_cols','tiling'),('qsv.tile_rows','tiling');

INSERT INTO setting_scope VALUES
 ('qsv.preset','intel-b580-ihd26.2.2-qsv-av1',1,'4',0),
 ('qsv.b_strategy','intel-b580-ihd26.2.2-qsv-av1',1,'-1',1),
 ('qsv.adaptive_b','intel-b580-ihd26.2.2-qsv-av1',1,'-1',0),
 ('generic.compression_level','intel-b580-ihd26.2.2-qsv-av1',1,'0',0);

INSERT INTO constant (name, value, unit, provenance, inputs_json, precision, reason, cites_json) VALUES
 ('CEILING',22.0,'Mbps muxed','derived','{"device_bytes":512e9,"hours":50}','±~10%',NULL,'["RESULTS §16"]'),
 ('MARGIN',0.20,'fraction','policy',NULL,NULL,'the remux skip threshold; bounded below by the probe precision (median SE 14.1%), RESULTS §19.8','["RESULTS §19.8"]'),
 ('HEADROOM',NULL,'fraction of CEILING','measured',NULL,NULL,NULL,'["RESULTS §18.6b"]'),
 ('BOUND',NULL,'qp','measured',NULL,NULL,NULL,'["RESULTS §18.6a"]'),
 ('RUNG_FACTOR',NULL,'x on bitrate per rung','measured',NULL,NULL,NULL,'["RESULTS §18.6a"]'),
 ('HOST_THRESHOLD',NULL,'Mbps','measured',NULL,NULL,NULL,'["RESULTS §18.5a"]');

INSERT INTO lane VALUES
 ('kids-ipad-standard-sdr','hevc','incumbent',NULL,NULL,'sdr','1080p','sdr','n/a','all default-flagged, else stream 0','text -> mov_text',NULL,1250,'never',NULL,1,NULL),
 ('kids-ipad-standard-hdr','hevc','incumbent',NULL,NULL,'hdr','1080p','sdr','tonemapping','all default-flagged, else stream 0','text -> mov_text',NULL,1250,'never',NULL,1,NULL),
 ('kids-ipad-2d-animation-sdr','hevc','incumbent',NULL,NULL,'sdr','native','sdr','n/a','all default-flagged, else stream 0','text -> mov_text',NULL,1250,'never',NULL,1,NULL),
 ('kids-ipad-2d-animation-hdr','hevc','incumbent',NULL,NULL,'hdr','native','sdr','tonemapping','all default-flagged, else stream 0','text -> mov_text',NULL,1250,'never',NULL,0,NULL),
 ('m4-ipad-le1080p-sdr','av1','cap',NULL,1920,'sdr','native','sdr','n/a','all default-flagged, else stream 0','text -> mov_text',NULL,1548,'rarely','CEILING',1,NULL),
 ('m4-ipad-le1080p-hdr','hevc','cap',NULL,1920,'hdr','native','hdr','passthrough','all default-flagged, else stream 0','text -> mov_text',NULL,1548,'rarely','CEILING',0,NULL),
 ('m4-ipad-gt1080p-sdr','hevc','cap',1921,NULL,'sdr','native','sdr','n/a','all default-flagged, else stream 0','text -> mov_text',NULL,1548,'always','CEILING',1,NULL),
 ('m4-ipad-gt1080p-hdr','hevc','cap',1921,NULL,'hdr','native','hdr','passthrough','all default-flagged, else stream 0','text -> mov_text',NULL,1548,'always','CEILING',1,NULL);

INSERT INTO lane_step VALUES
 ('kids-ipad-standard-sdr','quality-target-encode'),('kids-ipad-standard-hdr','quality-target-encode'),
 ('kids-ipad-2d-animation-sdr','quality-target-encode'),('kids-ipad-2d-animation-hdr','quality-target-encode'),
 ('m4-ipad-le1080p-sdr','probe'),('m4-ipad-le1080p-sdr','remux'),('m4-ipad-le1080p-sdr','quality-target-encode'),('m4-ipad-le1080p-sdr','bitrate-target-encode'),
 ('m4-ipad-le1080p-hdr','probe'),('m4-ipad-le1080p-hdr','remux'),('m4-ipad-le1080p-hdr','quality-target-encode'),('m4-ipad-le1080p-hdr','bitrate-target-encode'),
 ('m4-ipad-gt1080p-sdr','probe'),('m4-ipad-gt1080p-sdr','remux'),('m4-ipad-gt1080p-sdr','quality-target-encode'),('m4-ipad-gt1080p-sdr','bitrate-target-encode'),
 ('m4-ipad-gt1080p-hdr','probe'),('m4-ipad-gt1080p-hdr','remux'),('m4-ipad-gt1080p-hdr','quality-target-encode'),('m4-ipad-gt1080p-hdr','bitrate-target-encode');

INSERT INTO constant_scope VALUES
 ('CEILING','m4-ipad-le1080p-sdr'),('CEILING','m4-ipad-le1080p-hdr'),('CEILING','m4-ipad-gt1080p-sdr'),('CEILING','m4-ipad-gt1080p-hdr'),
 ('MARGIN','m4-ipad-le1080p-sdr'),('MARGIN','m4-ipad-le1080p-hdr'),('MARGIN','m4-ipad-gt1080p-sdr'),('MARGIN','m4-ipad-gt1080p-hdr'),
 ('BOUND','m4-ipad-le1080p-hdr'),('BOUND','m4-ipad-gt1080p-sdr'),('BOUND','m4-ipad-gt1080p-hdr'),
 ('RUNG_FACTOR','m4-ipad-le1080p-hdr'),('RUNG_FACTOR','m4-ipad-gt1080p-sdr'),('RUNG_FACTOR','m4-ipad-gt1080p-hdr'),
 ('HOST_THRESHOLD','m4-ipad-gt1080p-sdr'),('HOST_THRESHOLD','m4-ipad-gt1080p-hdr'),
 ('HEADROOM','m4-ipad-le1080p-sdr'),('HEADROOM','m4-ipad-le1080p-hdr'),('HEADROOM','m4-ipad-gt1080p-sdr'),('HEADROOM','m4-ipad-gt1080p-hdr');

INSERT INTO ladder VALUES ('hevc','hevc'),('av1','av1');
INSERT INTO ladder_rung
 SELECT 'hevc', value FROM (SELECT 4 AS value UNION SELECT 6 UNION SELECT 8 UNION SELECT 10 UNION SELECT 11 UNION SELECT 14 UNION SELECT 15 UNION SELECT 16 UNION SELECT 17 UNION SELECT 18 UNION SELECT 20 UNION SELECT 22 UNION SELECT 26 UNION SELECT 28 UNION SELECT 30 UNION SELECT 32 UNION SELECT 34 UNION SELECT 36 UNION SELECT 38 UNION SELECT 42 UNION SELECT 46);
INSERT INTO ladder_rung
 SELECT 'av1', value FROM (SELECT 15 AS value UNION SELECT 20 UNION SELECT 22 UNION SELECT 24 UNION SELECT 25 UNION SELECT 26 UNION SELECT 28 UNION SELECT 30 UNION SELECT 34 UNION SELECT 35 UNION SELECT 40 UNION SELECT 45 UNION SELECT 50 UNION SELECT 55 UNION SELECT 60);

-- the population: the 1080p lane's seven, plus one HDR 4K and one SDR 4K so every has_content lane is non-empty
INSERT INTO title (title_id, path, library, width, height, dynamic_range, dv_profile, video_codec, field_order, fps, bit_depth, bitrate_kbps, bpp, source_type, scanned_at) VALUES
 ('mrrobot','/mnt/storage/TV Shows/Mr. Robot/S01E01.mkv','TV Shows',1920,1080,'sdr',NULL,'h264','progressive',23.976,8,29200,0.587,'remux','2026-08-24'),
 ('parks','/mnt/storage/TV Shows/Parks and Recreation/S03E01.mkv','TV Shows',1920,1080,'sdr',NULL,'h264','progressive',23.976,8,12200,0.245,'web','2026-08-24'),
 ('shield','/mnt/storage/TV Shows/Agents of SHIELD/S01E01.mkv','TV Shows',1920,1080,'sdr',NULL,'h264','progressive',23.976,8,27400,0.551,'remux','2026-08-24'),
 ('snowpiercer','/mnt/storage/Movies/Snowpiercer (2013)/Snowpiercer.mkv','Movies',1920,1080,'sdr',NULL,'h264','progressive',23.976,8,39000,0.784,'remux','2026-08-24'),
 ('sopranos','/mnt/storage/TV Shows/The Sopranos/S01E01.mkv','TV Shows',1920,1080,'sdr',NULL,'h264','progressive',23.976,8,29800,0.599,'remux','2026-08-24'),
 ('tng','/mnt/storage/TV Shows/Star Trek TNG/S05E01.mkv','TV Shows',1920,1080,'sdr',NULL,'h264','progressive',23.976,8,23200,0.467,'remux','2026-08-24'),
 ('tos','/mnt/storage/TV Shows/Star Trek TOS/S02E01.mkv','TV Shows',1920,1080,'sdr',NULL,'vc1','progressive',23.976,8,18800,0.378,'bluray','2026-08-24'),
 ('interstellar','/mnt/storage/Movies/Interstellar (2014)/Interstellar.mkv','Movies',3840,2160,'hdr10',NULL,'hevc','progressive',23.976,10,50000,0.251,'remux','2026-08-24'),
 ('fixture-4k-sdr','/mnt/storage/Movies/Fixture (2020)/Fixture.mkv','Movies',3840,2160,'sdr',NULL,'hevc','progressive',23.976,10,40000,0.201,'remux','2026-08-24');

INSERT INTO window VALUES
 ('mrrobot','mrrobot',1201.0,60.0,'dark and noisy','pinned','satavg+cuts v1',88.2,'darkest and noisiest in the library'),
 ('parks','parks',402.5,60.0,'well-lit grain-free','pinned','satavg+cuts v1',73.5,'flat fluorescent light; keeps the set off all-REMUX'),
 ('shield','shield',1510.0,60.0,'sustained motion','pinned','satavg+cuts v1',80.1,'modern VFX and sustained motion'),
 ('snowpiercer','snowpiercer',3300.0,60.0,'sustained darkness','pinned','satavg+cuts v1',61.0,'hard-content probe'),
 ('sopranos','sopranos',900.0,60.0,'film grain','pinned','satavg+cuts v1',70.4,'dropped from the 1080p class: drama shot on film is not what the lane carries'),
 ('tng','tng',1400.0,60.0,'film grain','pinned','satavg+cuts v1',66.7,'1980s film scan, heavy grain'),
 ('tos','tos',777.0,60.0,'film grain','pinned','satavg+cuts v1',64.0,'the only non-h264 in the set; no VC-1 decoder on the B580');

INSERT INTO chain VALUES
 ('m4-ipad-le1080p-sdr','media-01','null',NULL),
 ('m4-ipad-le1080p-sdr','eta','null',NULL),
 ('m4-ipad-gt1080p-sdr','media-01','null',NULL),
 ('kids-ipad-2d-animation-hdr','media-01','hwupload_cuda,tonemap_cuda=...,scale_cuda=...',NULL);

INSERT INTO reference_set VALUES ('stage-1080p','1920x1080','p010le','media-01','8.1.2-Jellyfin 0b0ea2d','2026-08-25');

INSERT INTO content_class VALUES ('native-1080p-sdr','native-1080p-sdr','stage-1080p','1080p SDR live action, the m4-ipad-le1080p-sdr lane');
INSERT INTO content_class_lane VALUES ('native-1080p-sdr','m4-ipad-le1080p-sdr');
INSERT INTO content_class_member VALUES
 ('native-1080p-sdr','mrrobot'),('native-1080p-sdr','parks'),('native-1080p-sdr','shield'),
 ('native-1080p-sdr','snowpiercer'),('native-1080p-sdr','tng'),('native-1080p-sdr','tos');
INSERT INTO content_class_stratum VALUES
 ('native-1080p-sdr','remux','inventory',"t.source_type = 'remux'",1,NULL),
 ('native-1080p-sdr','web','inventory',"t.source_type = 'web'",1,NULL),
 ('native-1080p-sdr','non-h264 decode','inventory',"t.video_codec <> 'h264'",1,NULL),
 ('native-1080p-sdr','bpp p90','quantile','t.bpp >= 0.55',1,NULL),
 ('native-1080p-sdr','dark and noisy','character','dark and noisy',1,0.40),
 ('native-1080p-sdr','well-lit grain-free','character','well-lit grain-free',1,0.20);

INSERT INTO cut (cut_id, reference_set_id, window_id, kind, chain_lane, chain_host, content_sha, bytes, frames, tags_pinned)
 SELECT window_id || '.ref', 'stage-1080p', window_id, 'reference', 'm4-ipad-le1080p-sdr', 'media-01', 'sha-' || window_id || '-ref', 2000000000, 1439, NULL FROM window;
INSERT INTO cut (cut_id, reference_set_id, window_id, kind, chain_lane, chain_host, content_sha, bytes, frames, tags_pinned)
 SELECT window_id || '.src', 'stage-1080p', window_id, 'source', NULL, NULL, 'sha-' || window_id || '-src', 200000000, 1439, NULL FROM window;
INSERT INTO cut_check SELECT cut_id, 'content', 'pass', NULL, '2026-08-26' FROM cut WHERE kind = 'reference';

INSERT INTO search VALUES ('b580-qsv-av1','native-1080p-sdr','intel-b580-ihd26.2.2-qsv-av1','qsv.q',1548,'the first settings search; k = 3 factors, full factorial, preset swept',NULL);
INSERT INTO arm VALUES ('arm-a','b580-qsv-av1','cqp-preset4-bs0','base',NULL,NULL),('arm-b','b580-qsv-av1','cqp-preset4-bs1','candidate',NULL,NULL),
 ('arm-c','b580-qsv-av1','cqp-preset1-bs0','candidate',NULL,NULL);
-- the incumbent: what shipped before the search, preset 1 with B-pyramid, pinned at q 30. Its settings equal no other
-- arm's, so the incumbent checks have a positive instance of the shape that needs its OWN cells (accepted_by_viewing is
-- set once the viewing exists, below)
INSERT INTO arm VALUES ('arm-i','b580-qsv-av1','cqp-preset1-bs1-incumbent','incumbent',NULL,'30');
UPDATE search SET shipping_arm_id = 'arm-a' WHERE search_id = 'b580-qsv-av1';   -- the base ships: no candidate beat it
INSERT INTO arm_setting VALUES ('arm-a','qsv.preset','4'),('arm-a','qsv.b_strategy','0'),('arm-b','qsv.preset','4'),('arm-b','qsv.b_strategy','1'),
 ('arm-c','qsv.preset','1'),('arm-c','qsv.b_strategy','0'),('arm-i','qsv.preset','1'),('arm-i','qsv.b_strategy','1');
INSERT INTO search_coarse_rung SELECT 'b580-qsv-av1', value FROM (SELECT 12 AS value UNION SELECT 18 UNION SELECT 24 UNION SELECT 30 UNION SELECT 36 UNION SELECT 42);
INSERT INTO search_target VALUES ('b580-qsv-av1','ssimulacra2','mean',75,NULL),('b580-qsv-av1','ssimulacra2','mean',80,NULL),('b580-qsv-av1','ssimulacra2','mean',85,NULL);

-- every run names the artifact its plan was built for; the fixture's is one image digest
INSERT INTO run (run_id, encoder_unit_id, content_class_id, search_id, host, node_label, stage, artifact, ffmpeg_build, ffmpeg_sha,
                 scorer_build, ffvship_version, metric_backend, harness_version, started_at, finished_at) VALUES
 ('b580-qsv-av1','intel-b580-ihd26.2.2-qsv-av1','native-1080p-sdr','b580-qsv-av1','media-01','media-01-b580','encode','sweep-node@sha256:0a1b2c','8.1.2-Jellyfin','0b0ea2d','ffvship 1.3','1.3','libvmaf_cuda','g0',"2026-09-02T10:00",'2026-09-03T02:00'),
 ('b580-qsv-av1-time','intel-b580-ihd26.2.2-qsv-av1','native-1080p-sdr','b580-qsv-av1','media-01','media-01-b580','time','sweep-node@sha256:0a1b2c','8.1.2-Jellyfin','0b0ea2d',NULL,NULL,NULL,'g0','2026-09-03T10:00','2026-09-03T12:00'),
 ('b580-qsv-av1-screen','intel-b580-ihd26.2.2-qsv-av1','native-1080p-sdr',NULL,'media-01','media-01-b580','screen','sweep-node@sha256:0a1b2c','8.1.2-Jellyfin','0b0ea2d',NULL,NULL,NULL,'g0','2026-09-01T10:00','2026-09-01T11:03'),
 ('b580-qsv-av1-locate','intel-b580-ihd26.2.2-qsv-av1','native-1080p-sdr','b580-qsv-av1','media-01','media-01-b580','locate','sweep-node@sha256:0a1b2c','8.1.2-Jellyfin','0b0ea2d',NULL,NULL,NULL,'g0','2026-09-02T00:00','2026-09-02T00:40'),
 ('b580-viewing','intel-b580-ihd26.2.2-qsv-av1','native-1080p-sdr',NULL,'media-01','media-01-b580','viewing','sweep-node@sha256:0a1b2c','8.1.2-Jellyfin','0b0ea2d',NULL,NULL,NULL,'g0','2026-09-04T10:00','2026-09-04T10:10'),
 ('m4-calibrate','nvidia-a4000-595-nvenc-hevc',NULL,NULL,'media-01','media-01','calibrate','sweep-node@sha256:0a1b2c','8.1.2-Jellyfin','0b0ea2d',NULL,NULL,NULL,'g0','2026-08-28T10:00','2026-08-28T14:00');
-- HEADROOM 0.98 is the fixture's stand-in: the incumbent request factor, which overshoots and is unmeasured
INSERT INTO constant_value VALUES
 ('HEADROOM','m4-calibrate',0.98,'2026-08-28'),('BOUND','m4-calibrate',20,'2026-08-28'),
 ('RUNG_FACTOR','m4-calibrate',0.8374,'2026-08-28'),('HOST_THRESHOLD','m4-calibrate',10.0,'2026-08-28');
UPDATE run SET state = 'complete', fetched_at = finished_at, verified_at = finished_at;
-- the hub launches at claim and completes after verifying; the agent reports running
INSERT INTO run_event SELECT run_id, started_at, 'launched', 'claimed; the agent reports the artifact the plan names', 'hub' FROM run;
INSERT INTO run_event SELECT run_id, started_at || ':30', 'running', 'first cell started', 'agent' FROM run;
INSERT INTO run_event SELECT run_id, finished_at, 'complete', 'count and heights verified against the plan', 'hub' FROM run;
INSERT INTO run_window SELECT 'b580-qsv-av1', window_id FROM content_class_member WHERE content_class_id = 'native-1080p-sdr';
INSERT INTO run_window SELECT 'b580-qsv-av1-time', window_id FROM content_class_member WHERE content_class_id = 'native-1080p-sdr';
INSERT INTO run_window VALUES ('b580-qsv-av1-screen','tng');
INSERT INTO run_window SELECT 'b580-qsv-av1-locate', window_id FROM content_class_member WHERE content_class_id = 'native-1080p-sdr';
INSERT INTO run_window VALUES ('b580-viewing','tng');

-- two locate cells on the coarse ladder
INSERT INTO cell VALUES ('l-a24-tng','b580-qsv-av1-locate','tng','reference'),('l-a30-tng','b580-qsv-av1-locate','tng','reference');
INSERT INTO cell_setting VALUES ('l-a24-tng','qsv.preset','4','identity'),('l-a24-tng','qsv.b_strategy','0','identity'),('l-a24-tng','qsv.q','24','identity'),
 ('l-a30-tng','qsv.preset','4','identity'),('l-a30-tng','qsv.b_strategy','0','identity'),('l-a30-tng','qsv.q','30','identity');
INSERT INTO encode VALUES ('l-a24-tng',70000000,9300.0,1439,60.0,'hardware',0),('l-a30-tng',52000000,6900.0,1439,60.0,'hardware',0);

-- the derived ladders: per (arm, window), placed from the locate run, four rungs each
INSERT INTO arm_ladder_rung
 SELECT 'b580-qsv-av1-locate', a.arm_id, w.window_id, r.rung
   FROM arm a, (SELECT 'tng' AS window_id UNION SELECT 'parks') w,
        (SELECT 18 AS rung UNION SELECT 24 UNION SELECT 30 UNION SELECT 36) r
  WHERE a.role = 'candidate';

-- the base arm on every rung of the codec ladder INSIDE the anchor's range, on every member: the mandatory path
-- (13 x 6 cells -- qsv.q ends at 51, so the ladder's 55 and 60 are UNREACHABLE on this unit and have no cell);
-- plus one candidate at two rungs on two windows: the search, when the screen earned it
INSERT INTO cell SELECT 'c-a' || r.rung || '-' || m.window_id, 'b580-qsv-av1', m.window_id, 'reference'
  FROM ladder_rung r, content_class_member m, setting s
 WHERE r.ladder_id = 'av1' AND m.content_class_id = 'native-1080p-sdr' AND s.setting_id = 'qsv.q' AND r.rung BETWEEN s.range_lo AND s.range_hi;
INSERT INTO cell_setting SELECT 'c-a' || r.rung || '-' || m.window_id, 'qsv.q', CAST(r.rung AS TEXT), 'identity'
  FROM ladder_rung r, content_class_member m, setting s
 WHERE r.ladder_id = 'av1' AND m.content_class_id = 'native-1080p-sdr' AND s.setting_id = 'qsv.q' AND r.rung BETWEEN s.range_lo AND s.range_hi;
INSERT INTO cell VALUES
 ('c-b24-tng','b580-qsv-av1','tng','reference'),('c-b30-tng','b580-qsv-av1','tng','reference'),
 ('c-b24-parks','b580-qsv-av1','parks','reference'),('c-b30-parks','b580-qsv-av1','parks','reference');
INSERT INTO cell_setting SELECT cell_key, 'qsv.q', CASE WHEN cell_key LIKE '%24%' THEN '24' ELSE '30' END, 'identity' FROM cell WHERE run_id = 'b580-qsv-av1' AND cell_key LIKE 'c-b%';
INSERT INTO cell VALUES ('c-b36-tng','b580-qsv-av1','tng','reference');
INSERT INTO cell_setting VALUES ('c-b36-tng','qsv.q','36','identity');
INSERT INTO cell_failure VALUES ('c-b36-tng','2026-09-02T14:00','[av1_qsv @ 0x...] Error initializing the encoder: unsupported (-3)',218);
INSERT INTO cell_setting SELECT cell_key, 'qsv.preset', '4', 'identity' FROM cell WHERE run_id = 'b580-qsv-av1';
INSERT INTO cell_setting SELECT cell_key, 'qsv.b_strategy', CASE WHEN cell_key LIKE 'c-a%' THEN '0' ELSE '1' END, 'identity' FROM cell WHERE run_id = 'b580-qsv-av1';
INSERT INTO cell_setting SELECT cell_key, 'qsv.adaptive_b', '-1', 'default_resolved' FROM cell WHERE run_id = 'b580-qsv-av1';
INSERT INTO encode SELECT c.cell_key, 60000000, 30000.0 - 400.0 * CAST(cs.value AS REAL), 1439, 60.0, 'hardware', 0
  FROM cell c JOIN cell_setting cs ON cs.cell_key = c.cell_key AND cs.setting_id = 'qsv.q' WHERE c.run_id = 'b580-qsv-av1';
-- the incumbent arm at its pinned anchor on every member (Stage 5): the bar the incumbent rule reads, scored below with the rest
INSERT INTO cell SELECT 'c-i30-' || window_id, 'b580-qsv-av1', window_id, 'reference' FROM content_class_member WHERE content_class_id = 'native-1080p-sdr';
INSERT INTO cell_setting SELECT cell_key, 'qsv.q', '30', 'identity' FROM cell WHERE cell_key LIKE 'c-i30-%';
INSERT INTO cell_setting SELECT cell_key, 'qsv.preset', '1', 'identity' FROM cell WHERE cell_key LIKE 'c-i30-%';
INSERT INTO cell_setting SELECT cell_key, 'qsv.b_strategy', '1', 'identity' FROM cell WHERE cell_key LIKE 'c-i30-%';
INSERT INTO cell_setting SELECT cell_key, 'qsv.adaptive_b', '-1', 'default_resolved' FROM cell WHERE cell_key LIKE 'c-i30-%';
INSERT INTO encode SELECT cell_key, 34000000, 17000.0, 1439, 60.0, 'hardware', 0 FROM cell WHERE cell_key LIKE 'c-i30-%';
INSERT INTO score SELECT c.cell_key, 1548, 'ssimulacra2', 'mean', 95.0 - 0.5 * CAST(cs.value AS REAL), 'S1', 'FFVship 1.3 + 8.1.2-0b0ea2d'
  FROM cell c JOIN cell_setting cs ON cs.cell_key = c.cell_key AND cs.setting_id = 'qsv.q' WHERE c.run_id = 'b580-qsv-av1';
INSERT INTO score SELECT c.cell_key, 1548, 'ssimulacra2', 'p5', 86.0 - 0.5 * CAST(cs.value AS REAL), 'S1', 'FFVship 1.3 + 8.1.2-0b0ea2d'
  FROM cell c JOIN cell_setting cs ON cs.cell_key = c.cell_key AND cs.setting_id = 'qsv.q' WHERE c.run_id = 'b580-qsv-av1';
INSERT INTO score SELECT c.cell_key, 1548, 'ssimulacra2', 'min', 80.0 - 0.5 * CAST(cs.value AS REAL), 'S1', 'FFVship 1.3 + 8.1.2-0b0ea2d'
  FROM cell c JOIN cell_setting cs ON cs.cell_key = c.cell_key AND cs.setting_id = 'qsv.q' WHERE c.run_id = 'b580-qsv-av1';
INSERT INTO score SELECT cell_key, 1548, 'vmaf', 'mean', 96.0, 'S1', 'FFVship 1.3 + 8.1.2-0b0ea2d' FROM cell WHERE run_id = 'b580-qsv-av1';
INSERT INTO score SELECT cell_key, 1548, 'cambi', 'mean', 0.4, 'S1', 'FFVship 1.3 + 8.1.2-0b0ea2d' FROM cell WHERE run_id = 'b580-qsv-av1';
INSERT INTO score SELECT cell_key, 1548, 'butteraugli', 'max', 3.1, 'S1', 'FFVship 1.3 + 8.1.2-0b0ea2d' FROM cell WHERE run_id = 'b580-qsv-av1';
INSERT INTO step_trace VALUES ('c-a24-tng',1548,'rescale_ref',20.1,2.0,0.0,0.0),('c-a24-tng',1548,'libvmaf',51.4,11.0,15.9,22.0);

-- timing on the SOURCE cut: two hardware windows and the VC-1 one on the software path
INSERT INTO cell VALUES ('t-tng','b580-qsv-av1-time','tng','source'),('t-parks','b580-qsv-av1-time','parks','source'),('t-tos','b580-qsv-av1-time','tos','source');
INSERT INTO cell_setting VALUES ('t-tng','qsv.preset','4','identity'),('t-tng','qsv.b_strategy','0','identity'),('t-tng','qsv.q','24','identity'),
 ('t-parks','qsv.preset','4','identity'),('t-parks','qsv.b_strategy','0','identity'),('t-parks','qsv.q','24','identity'),
 ('t-tos','qsv.preset','4','identity'),('t-tos','qsv.b_strategy','0','identity'),('t-tos','qsv.q','24','identity');
INSERT INTO encode VALUES ('t-tng',60000000,8000.0,1439,60.0,'hardware',0),('t-parks',30000000,4000.0,1439,60.0,'hardware',0),('t-tos',40000000,5300.0,1439,60.0,'software',0);
INSERT INTO timing (cell_key, workers, repeat_index, fps, wall_s, decode_path, is_warmup, noise_floor_pct) VALUES
 ('t-tng',1,0,640.0,2.2,'hardware',1,2.8),('t-tng',1,1,690.0,2.1,'hardware',0,2.8),('t-tng',1,2,700.0,2.05,'hardware',0,2.8),
 ('t-parks',1,0,700.0,2.0,'hardware',1,2.8),('t-parks',1,1,720.0,2.0,'hardware',0,2.8),
 ('t-tos',1,0,60.0,24.0,'software',1,2.8),('t-tos',1,1,62.0,23.2,'software',0,2.8);

-- the screen, on ONE window: b_strategy HONOURED; adaptive_b INERT under a base that is itself measured
INSERT INTO cell VALUES ('s-bs0','b580-qsv-av1-screen','tng','reference'),('s-bs1','b580-qsv-av1-screen','tng','reference'),('s-ab1','b580-qsv-av1-screen','tng','reference');
INSERT INTO cell_setting VALUES ('s-bs0','qsv.b_strategy','0','identity'),('s-bs1','qsv.b_strategy','1','identity'),('s-ab1','qsv.adaptive_b','1','identity'),('s-ab1','qsv.b_strategy','1','identity'),
 ('s-bs0','qsv.q','30','identity'),('s-bs1','qsv.q','30','identity'),('s-ab1','qsv.q','30','identity');
INSERT INTO encode VALUES ('s-bs0',60000000,8000.0,1439,60.0,'hardware',0),('s-bs1',30000000,4000.0,1439,60.0,'hardware',0),('s-ab1',30000000,4000.0,1439,60.0,'hardware',0);
INSERT INTO setting_verdict VALUES
 (1,'intel-b580-ihd26.2.2-qsv-av1','qsv.b_strategy','tng','HONOURED',20.3,NULL,NULL,2.77,NULL),
 (2,'intel-b580-ihd26.2.2-qsv-av1','qsv.adaptive_b','tng','INERT',0.0,'qsv.b_strategy','1',2.77,NULL);
INSERT INTO cell VALUES ('s-p1','b580-qsv-av1-screen','tng','reference'),('s-p4','b580-qsv-av1-screen','tng','reference');
INSERT INTO cell_setting VALUES ('s-p1','qsv.preset','1','identity'),('s-p4','qsv.preset','4','identity'),('s-p1','qsv.q','30','identity'),('s-p4','qsv.q','30','identity');
INSERT INTO encode VALUES ('s-p1',63000000,8400.0,1439,60.0,'hardware',0),('s-p4',60000000,8000.0,1439,60.0,'hardware',0);
INSERT INTO setting_verdict VALUES (3,'intel-b580-ihd26.2.2-qsv-av1','qsv.preset','tng','HONOURED',5.01,NULL,NULL,2.77,NULL);
INSERT INTO setting_verdict_cell VALUES (1,'s-bs0'),(1,'s-bs1'),(2,'s-ab1'),(2,'s-bs1'),(3,'s-p1'),(3,'s-p4');

-- the viewing: two encodes KEPT for a person to view, and the verdict
INSERT INTO cell VALUES ('g-a','b580-viewing','tng','reference'),('g-b','b580-viewing','tng','reference'),('g-i','b580-viewing','tng','reference');
INSERT INTO cell_setting VALUES ('g-a','qsv.preset','4','identity'),('g-a','qsv.b_strategy','0','identity'),('g-a','qsv.q','30','identity'),
 ('g-b','qsv.preset','4','identity'),('g-b','qsv.b_strategy','0','identity'),('g-b','qsv.q','34','identity'),
 ('g-i','qsv.preset','1','identity'),('g-i','qsv.b_strategy','1','identity'),('g-i','qsv.q','30','identity');
INSERT INTO encode VALUES ('g-a',52000000,6900.0,1439,60.0,'hardware',1),('g-b',44000000,5900.0,1439,60.0,'hardware',1),('g-i',36000000,4800.0,1439,60.0,'hardware',1);
INSERT INTO viewing_verdict VALUES (1,'pair',NULL,'tng','g-a','g-b','iPad M4 13in','andy','same','not worth the size','2026-09-04');
-- an acceptance too, of the incumbent's own encode: the viewing the incumbent arm names, and a positive instance of BOTH kinds for the discarded-encode check
INSERT INTO viewing_verdict VALUES (9,'acceptance','m4-ipad-le1080p-sdr','tng','g-i',NULL,'iPad M4 13in','andy','acceptable','what ships today, at q 30, is acceptable for the lane','2026-09-04');
UPDATE arm SET accepted_by_viewing = 9 WHERE arm_id = 'arm-i';

-- shipping: a measured value on the class, a bitrate-target value computed from a constant,
-- a derived HEVC value, and a no-content value carried from the SDR lane
INSERT INTO shipped VALUES
 (1,'m4-ipad-le1080p-sdr','media-01','quality-target-encode','intel-b580-ihd26.2.2-qsv-av1','measured','measurement',NULL,'native-1080p-sdr',1,'SELECT ... FROM score WHERE ...','["RESULTS §4g-ii"]'),
 (2,'m4-ipad-le1080p-sdr','media-01','bitrate-target-encode','intel-b580-ihd26.2.2-qsv-av1','derived','measurement','-b:v is CEILING x HEADROOM; a whole-title VBV settles differently from a window',NULL,1,NULL,'["RESULTS §18.6b"]'),
 (3,'m4-ipad-gt1080p-sdr','media-01','quality-target-encode','nvidia-a4000-595-nvenc-hevc','derived','measurement','budget interpolation between scored qp14 and qp17; confirmed by eye twice',NULL,2,NULL,'["RESULTS §18.4"]'),
 (4,'kids-ipad-2d-animation-hdr','media-01','quality-target-encode','nvidia-a4000-595-nvenc-hevc','no-content','policy','the library has no HDR 2D animation; the SDR lane''s value so the flow has no hole',NULL,2,NULL,'["RESULTS §4a"]');
INSERT INTO shipped_setting VALUES
 (1,'qsv.preset','4','identity',NULL),(1,'qsv.b_strategy','0','identity',NULL),(1,'qsv.q','30','identity',NULL),
 (2,'qsv.preset','4','identity',NULL),(2,'qsv.b_strategy','0','identity',NULL),(2,'qsv.b_v','17600000','computed','CEILING'),
 (3,'nvenc.preset','p2','identity',NULL),(3,'nvenc.tune','uhq','identity',NULL),(3,'nvenc.rc','constqp','identity',NULL),(3,'nvenc.qp','15','identity',NULL),
 (4,'nvenc.preset','p3','identity',NULL),(4,'nvenc.cq','34','identity',NULL);
INSERT INTO shipped VALUES
 (5,'m4-ipad-le1080p-sdr','media-01','probe','nvidia-a4000-595-nvenc-hevc','derived','policy','the HEVC probe is pinned to qp 14 so it reads the same on every host',NULL,1,NULL,'["TDARR-TRANSCODE-PLAN.md rule 2"]'),
 (6,'m4-ipad-le1080p-sdr','media-01','remux',NULL,'fixed','policy','container rebuild only; no encoder decision',NULL,NULL,NULL,NULL),
 (7,'m4-ipad-gt1080p-sdr','media-01','probe','nvidia-a4000-595-nvenc-hevc','derived','policy','the HEVC probe is pinned to qp 14 so it reads the same on every host',NULL,1,NULL,NULL),
 (8,'m4-ipad-gt1080p-sdr','media-01','remux',NULL,'fixed','policy','container rebuild only; no encoder decision',NULL,NULL,NULL,NULL),
 (9,'m4-ipad-gt1080p-sdr','media-01','bitrate-target-encode','nvidia-a4000-595-nvenc-hevc','derived','measurement','-b:v is CEILING x HEADROOM',NULL,2,NULL,'["RESULTS §18.6b"]');
INSERT INTO shipped_setting VALUES
 (5,'nvenc.preset','p2','identity',NULL),(5,'nvenc.tune','uhq','identity',NULL),(5,'nvenc.rc','constqp','identity',NULL),(5,'nvenc.qp','14','identity',NULL),
 (7,'nvenc.preset','p2','identity',NULL),(7,'nvenc.tune','uhq','identity',NULL),(7,'nvenc.rc','constqp','identity',NULL),(7,'nvenc.qp','14','identity',NULL),
 (9,'nvenc.preset','p2','identity',NULL),(9,'nvenc.tune','uhq','identity',NULL),(9,'nvenc.rc','vbr','identity',NULL),(9,'nvenc.b_v','17600000','computed','CEILING');
INSERT INTO host_unit VALUES
 ('media-01','intel-b580-ihd26.2.2-qsv-av1','/dev/dri/by-path/pci-0000:03:00.0-render'),
 ('media-01','nvidia-a4000-595-nvenc-hevc','pci-0000:41:00.0'),
 ('eta','nvidia-5060ti-595-nvenc-av1','pci-0000:01:00.0');
INSERT INTO routing_exclusion VALUES
 ('kids-ipad-standard-sdr','media-01','fixture: the kids lanes are not modelled here'),
 ('kids-ipad-standard-hdr','media-01','fixture: the kids lanes are not modelled here'),
 ('kids-ipad-2d-animation-sdr','media-01','fixture: the kids lanes are not modelled here'),
 ('m4-ipad-gt1080p-hdr','media-01','fixture: not modelled here'),
 ('m4-ipad-le1080p-sdr','eta','eta is not yet measured on this class: the B580 column exists and eta has none');
"""


def load_fixture(conn):
    conn.executescript(FIXTURE)


# ---------------------------------------------------------------- checks

# name -> (what it refuses, the fix); the fix is the text after ` -- ` in the store's REFUSING reply
SCRIPT_CHECKS = OrderedDict([
    ("strata_covered", ("every inventory or quantile stratum has >= min_windows members satisfying its definition, "
                        "and every character stratum has >= min_windows members carrying it",
                        "define-class with enough members for every stratum's min_windows, or a stratum whose "
                        "min_windows the population can meet; a gap is a refusal, not a footnote")),
    ("measured_config_was_measured", ("a `measured` shipped row's identity settings equal some cell's identity "
                                      "settings, on the shipped unit, in the evidence class",
                                      "ship identity settings a cell in the evidence class was encoded with on that unit, "
                                      "or ship them as policy with the reason")),
    ("cells_match_an_arm", ("every cell in a run that executes a search has identity settings equal, minus the anchor, "
                            "to one of the search's arms -- exactly one base or candidate, or else the incumbent alone -- "
                            "so no cell is orphaned and no two swept arms share a configuration",
                            "plan cells from the search's arms only; two arms with one configuration are one arm")),
    ("incumbent_viewing_matches_arm", ("the acceptance viewing an incumbent arm names viewed an encode whose identity settings "
                                       "equal the arm's plus its pinned anchor",
                                       "author-search with the incumbent's settings and pinned anchor equal to the encode the "
                                       "acceptance viewing viewed")),
    ("shipping_arm_ladder_complete", ("the arm that ships has every rung of the codec ladder inside the anchor's range encoded "
                                      "on every member of the class, so any rung that ships was measured; a rung past the range "
                                      "is UNREACHABLE, not missing",
                                      "encode the shipping arm at every in-range rung on every member of the class before "
                                      "set-shipping-arm")),
    ("incumbent_arm_scored", ("an incumbent arm is encoded at its pinned anchor on every member of the class and scored at the "
                              "search's height, so the bar the incumbent rule reads was measured on this class and unit; "
                              "the cell may be the base arm's",
                              "encode and score the incumbent at its pinned anchor on every member of the class before "
                              "set-shipping-arm")),
    ("content_rate_meets_floor", ("content minutes per wall minute per (lane, host) at the shipped setting and worker count "
                                  "meets the lane's floor; a floor with no timing behind it is UNMEASURED, not unchanged",
                                  "time the shipped setting on that (lane, host) at that worker count; a rate under the floor "
                                  "ships elsewhere, or the floor changes with set-floor")),
    ("tags_complete", ("every table carries @group, @class and @writer; FILE means a person wrote it; one writer per table",
                       "tag the table in sweep/schema.sql with @group, @class and @writer")),
])

DDL_ENFORCED = [
    "a score row always names its height; a timing row always names its decode path and worker count",
    "a shipped row's (lane, step) is one of the lane's steps",
    "`measured` needs an evidence class; `policy` needs a reason; a `classified` cut check needs a reason",
    "a derived constant carries its inputs and its precision; a policy constant carries its value and its reason; a measured one has no typed value",
    "an incumbent arm is pinned at its anchor; the base and the candidates are not",
    "a score target and its height are set together or not at all; a cap that binds names its constant",
    "one ladder per codec; a reference cut names the chain that built it",
]


def check_strata(conn):
    out = []
    for cc, stratum, kind, definition, min_w in conn.execute(
            "SELECT content_class_id, stratum, kind, definition, min_windows FROM content_class_stratum"):
        if kind == "character":
            n = conn.execute("SELECT count(*) FROM content_class_member m JOIN window w ON w.window_id = m.window_id "
                             "WHERE m.content_class_id = ? AND w.character = ?", (cc, definition)).fetchone()[0]
        else:
            n = conn.execute(f"SELECT count(*) FROM content_class_member m JOIN window w ON w.window_id = m.window_id "
                             f"JOIN title t ON t.title_id = w.title_id WHERE m.content_class_id = ? AND ({definition})",
                             (cc,)).fetchone()[0]
        if n < min_w:
            out.append((cc, stratum, kind, n, min_w))
    return out


def check_measured_configs(conn):
    out = []
    for sid, lane, unit, cc in conn.execute(
            "SELECT shipped_id, lane, encoder_unit_id, content_class_id FROM shipped WHERE provenance = 'measured'"):
        want = frozenset(conn.execute("SELECT setting_id, value FROM shipped_setting WHERE shipped_id = ? AND role = 'identity'",
                                      (sid,)).fetchall())
        found = False
        for (ck,) in conn.execute("SELECT c.cell_key FROM cell c JOIN run r ON r.run_id = c.run_id "
                                  "WHERE r.encoder_unit_id = ? AND r.content_class_id = ?", (unit, cc)):
            have = frozenset(conn.execute("SELECT setting_id, value FROM cell_setting WHERE cell_key = ? AND role = 'identity'",
                                          (ck,)).fetchall())
            if have == want:
                found = True
                break
        if not found:
            out.append((sid, lane, sorted(want)))
    return out


def check_cells_match_arms(conn):
    """A cell in a search run is one arm's: its identity settings minus the anchor equal that arm's. The incumbent's
    cells count too -- Stage 5 encodes them precisely when its settings equal no base or candidate's -- and a cell
    equal to the base's and the incumbent's at once is the base arm's, so an incumbent match never makes a surplus."""
    out = []
    for run_id, search_id, anchor in conn.execute(
            "SELECT r.run_id, r.search_id, s.anchor_setting_id FROM run r JOIN search s ON s.search_id = r.search_id"):
        arms = {arm_id: (role, frozenset(conn.execute("SELECT setting_id, value FROM arm_setting WHERE arm_id = ?", (arm_id,)).fetchall()))
                for arm_id, role in conn.execute("SELECT arm_id, role FROM arm WHERE search_id = ?", (search_id,))}
        for (ck,) in conn.execute("SELECT cell_key FROM cell WHERE run_id = ?", (run_id,)):
            have = frozenset(conn.execute("SELECT setting_id, value FROM cell_setting WHERE cell_key = ? AND role = 'identity' "
                                          "AND setting_id <> ?", (ck, anchor)).fetchall())
            matches = [a for a, (_, s) in arms.items() if s == have]
            swept = [a for a in matches if arms[a][0] != 'incumbent']
            if len(swept) > 1 or (not swept and len(matches) != 1):
                out.append((run_id, ck, matches))
    return out


def check_incumbent_viewings(conn):
    out = []
    for arm_id, viewing_id, anchor, anchor_value in conn.execute(
            "SELECT a.arm_id, a.accepted_by_viewing, s.anchor_setting_id, a.anchor_value FROM arm a "
            "JOIN search s ON s.search_id = a.search_id WHERE a.role = 'incumbent' AND a.accepted_by_viewing IS NOT NULL"):
        want = frozenset(conn.execute("SELECT setting_id, value FROM arm_setting WHERE arm_id = ?", (arm_id,)).fetchall()) \
            | {(anchor, anchor_value)}
        (cell_a,), = conn.execute("SELECT cell_a FROM viewing_verdict WHERE viewing_id = ?", (viewing_id,)).fetchall()
        have = frozenset(conn.execute("SELECT setting_id, value FROM cell_setting WHERE cell_key = ? AND role = 'identity'",
                                      (cell_a,)).fetchall())
        if have != want:
            out.append((arm_id, viewing_id, sorted(want), sorted(have)))
    return out


def check_shipping_arm_ladders(conn):
    out = []
    for search_id, cc, unit, anchor, ship in conn.execute(
            "SELECT search_id, content_class_id, encoder_unit_id, anchor_setting_id, shipping_arm_id FROM search"):
        arm = ship or (conn.execute("SELECT arm_id FROM arm WHERE search_id = ? AND role = 'base'", (search_id,)).fetchone() or [None])[0]
        if arm is None:
            out.append((search_id, "no base arm"))
            continue
        settings = frozenset(conn.execute("SELECT setting_id, value FROM arm_setting WHERE arm_id = ?", (arm,)).fetchall())
        (codec,) = conn.execute("SELECT codec FROM encoder_unit WHERE encoder_unit_id = ?", (unit,)).fetchone()
        lo, hi = conn.execute("SELECT range_lo, range_hi FROM setting WHERE setting_id = ?", (anchor,)).fetchone()
        rungs = [r for (r,) in conn.execute("SELECT rung FROM ladder_rung lr JOIN ladder l ON l.ladder_id = lr.ladder_id WHERE l.codec = ?", (codec,))
                 if (lo is None or r >= lo) and (hi is None or r <= hi)]   # a rung past the anchor's range is UNREACHABLE, never a cell
        members = [w for (w,) in conn.execute("SELECT window_id FROM content_class_member WHERE content_class_id = ?", (cc,))]
        have = {}
        for ck, w in conn.execute("SELECT c.cell_key, c.window_id FROM cell c JOIN run r ON r.run_id = c.run_id "
                                  "WHERE r.search_id = ? AND r.stage = 'encode'", (search_id,)):
            ident = frozenset(conn.execute("SELECT setting_id, value FROM cell_setting WHERE cell_key = ? AND role = 'identity'", (ck,)).fetchall())
            have.setdefault(w, set()).add(ident)
        missing = [(w, r) for w in members for r in rungs if (settings | {(anchor, str(r))}) not in have.get(w, set())]
        if missing:
            out.append((search_id, arm, len(missing), missing[:3]))
    return out


def check_incumbent_arms_scored(conn):
    """The bar the incumbent rule reads must exist: a scored cell at the pinned anchor on every member."""
    out = []
    for arm_id, search_id, cc, unit, anchor, anchor_value, height in conn.execute(
            "SELECT a.arm_id, s.search_id, s.content_class_id, s.encoder_unit_id, s.anchor_setting_id, a.anchor_value, "
            "s.score_height FROM arm a JOIN search s ON s.search_id = a.search_id WHERE a.role = 'incumbent'"):
        want = frozenset(conn.execute("SELECT setting_id, value FROM arm_setting WHERE arm_id = ?", (arm_id,)).fetchall()) \
            | {(anchor, anchor_value)}
        missing = []
        for (w,) in conn.execute("SELECT window_id FROM content_class_member WHERE content_class_id = ?", (cc,)):
            found = False
            for (ck,) in conn.execute("SELECT c.cell_key FROM cell c JOIN run r ON r.run_id = c.run_id "
                                      "WHERE r.encoder_unit_id = ? AND r.content_class_id = ? AND c.window_id = ? "
                                      "AND c.cut_kind = 'reference'", (unit, cc, w)):
                have = frozenset(conn.execute("SELECT setting_id, value FROM cell_setting WHERE cell_key = ? AND role = 'identity'",
                                              (ck,)).fetchall())
                if have == want and conn.execute("SELECT 1 FROM score WHERE cell_key = ? AND height = ?", (ck, height)).fetchone():
                    found = True
                    break
            if not found:
                missing.append(w)
        if missing:
            out.append((arm_id, search_id, missing))
    return out


def check_content_rates(conn):
    out = []
    for sid, lane, host, unit, workers, floor in conn.execute(
            "SELECT s.shipped_id, s.lane, s.host, s.encoder_unit_id, coalesce(s.workers, 1), l.min_content_rate "
            "FROM shipped s JOIN lane l ON l.lane = s.lane "
            "WHERE s.step IN ('quality-target-encode','bitrate-target-encode') AND s.encoder_unit_id IS NOT NULL"):
        if floor is None:
            continue                      # a lane with no floor is reported, never refused
        ident = frozenset(conn.execute(
            "SELECT ss.setting_id, ss.value FROM shipped_setting ss JOIN setting st ON st.setting_id = ss.setting_id "
            "WHERE ss.shipped_id = ? AND ss.role = 'identity' AND st.kind <> 'quality_anchor'", (sid,)).fetchall())
        rates = []
        for ck, title_fps in conn.execute(
                "SELECT c.cell_key, t.fps FROM cell c JOIN run r ON r.run_id = c.run_id "
                "JOIN content_class_lane cl ON cl.content_class_id = r.content_class_id AND cl.lane = ? "
                "JOIN window w ON w.window_id = c.window_id JOIN title t ON t.title_id = w.title_id "
                "WHERE r.encoder_unit_id = ? AND c.cut_kind = 'source'", (lane, unit)):
            have = frozenset(conn.execute(
                "SELECT cs.setting_id, cs.value FROM cell_setting cs JOIN setting st ON st.setting_id = cs.setting_id "
                "WHERE cs.cell_key = ? AND cs.role = 'identity' AND st.kind <> 'quality_anchor'", (ck,)).fetchall())
            if have != ident:
                continue
            for (fps,) in conn.execute("SELECT fps FROM timing WHERE cell_key = ? AND workers = ? AND decode_path = 'hardware' "
                                       "AND is_warmup = 0", (ck, workers)):
                rates.append(fps * workers / title_fps)
        if not rates:
            out.append((sid, lane, host, "UNMEASURED: no timing at the shipped setting and worker count"))
            continue
        rates.sort()
        median = rates[len(rates) // 2]
        if median < floor:
            out.append((sid, lane, host, f"{median:.1f} content-min/min, under the floor {floor}"))
    return out


def run_checks(conn, tags):
    """{check_name: [violations]} over every x_ view and every script check."""
    results = OrderedDict()
    for v in db_views(conn, "x_"):
        results[v] = conn.execute(f"SELECT * FROM {v}").fetchall()
    results["strata_covered"] = check_strata(conn)
    results["measured_config_was_measured"] = check_measured_configs(conn)
    results["cells_match_an_arm"] = check_cells_match_arms(conn)
    results["incumbent_viewing_matches_arm"] = check_incumbent_viewings(conn)
    results["shipping_arm_ladder_complete"] = check_shipping_arm_ladders(conn)
    results["incumbent_arm_scored"] = check_incumbent_arms_scored(conn)
    results["content_rate_meets_floor"] = check_content_rates(conn)
    results["tags_complete"] = check_tags(conn, tags)
    return results


# ---------------------------------------------------------------- negative cases: each must be CAUGHT

MUTATIONS = [
    ("a shipped anchor off the ladder", "UPDATE shipped_setting SET value = '31' WHERE shipped_id = 1 AND setting_id = 'qsv.q'",
     "x_shipped_not_a_rung"),
    ("a measured value on content outside the lane",
     "UPDATE title SET width = 3840 WHERE title_id IN "
     "(SELECT w.title_id FROM content_class_member m JOIN window w ON w.window_id = m.window_id)",
     "x_measured_without_representation"),
    ("a measured value citing a class not sampled for the lane",
     "INSERT INTO content_class VALUES ('other','other','stage-1080p',NULL); "
     "INSERT INTO content_class_lane VALUES ('other','m4-ipad-gt1080p-sdr'); "
     "INSERT INTO content_class_member VALUES ('other','tng'); "
     "UPDATE shipped SET content_class_id = 'other' WHERE shipped_id = 1",
     "x_measured_on_a_class_not_for_the_lane"),
    ("a constant used outside its scope", "DELETE FROM constant_scope WHERE name = 'CEILING' AND lane = 'm4-ipad-le1080p-sdr'",
     "x_constant_outside_scope"),
    ("a run covering a window outside its class", "INSERT INTO run_window VALUES ('b580-qsv-av1', 'sopranos')",
     "x_run_outside_class"),
    ("a cell on a window the run never declared", "DELETE FROM run_window WHERE run_id = 'b580-qsv-av1' AND window_id = 'parks'",
     "x_cell_outside_run_coverage"),
    ("a setting value outside its enumeration", "UPDATE cell_setting SET value = '9' WHERE cell_key = 'c-a24-tng' AND setting_id = 'qsv.preset'",
     "x_setting_value_outside_enum"),
    ("a reference cut built with a chain the class does not serve",
     "INSERT INTO chain VALUES ('kids-ipad-standard-sdr','media-01','scale_cuda=...',NULL); "
     "UPDATE cut SET chain_lane = 'kids-ipad-standard-sdr' WHERE cut_id = 'tng.ref'",
     "x_cut_chain_not_a_served_lane"),
    ("a class member with no reference cut", "DELETE FROM cut_check WHERE cut_id = 'shield.ref'; DELETE FROM cut WHERE cut_id = 'shield.ref'",
     "x_member_without_reference_cut"),
    ("a class serving no lane", "DELETE FROM content_class_lane WHERE content_class_id = 'native-1080p-sdr'",
     "x_class_serves_no_lane"),
    ("a shipped encode with no chain on that host", "DELETE FROM chain WHERE lane = 'm4-ipad-gt1080p-sdr' AND host = 'media-01'",
     "x_shipped_without_chain"),
    ("a verdict under a base not measured to be honoured", "UPDATE setting_verdict SET verdict = 'INERT' WHERE setting_id = 'qsv.b_strategy'",
     "x_verdict_on_unmeasured_base"),
    ("a lane with content and an empty population", "DELETE FROM title WHERE title_id = 'fixture-4k-sdr'",
     "x_has_content_but_empty"),
    ("a stratum with no window", "UPDATE title SET source_type = 'remux' WHERE title_id = 'parks'",
     "strata_covered"),
    ("a measured configuration nobody encoded", "UPDATE shipped_setting SET value = '1' WHERE shipped_id = 1 AND setting_id = 'qsv.preset'",
     "measured_config_was_measured"),
    ("an arm using a setting never screened HONOURED", "INSERT INTO arm_setting VALUES ('arm-a', 'qsv.tile_cols', '2')",
     "x_arm_setting_not_honoured"),
    ("a ladder derived from a run that is not the locate", "UPDATE run SET stage = 'encode' WHERE run_id = 'b580-qsv-av1-locate'",
     "x_ladder_from_a_non_locate_run"),
    ("a ladder with three rungs", "DELETE FROM arm_ladder_rung WHERE arm_id = 'arm-b' AND window_id = 'tng' AND rung = 36",
     "x_ladder_below_floor"),
    ("a viewing verdict on a discarded encode", "UPDATE encode SET kept = 0 WHERE cell_key = 'g-a'",
     "x_viewing_on_a_discarded_encode"),
    ("a pair viewing whose second encode was discarded", "UPDATE encode SET kept = 0 WHERE cell_key = 'g-b'",
     "x_viewing_on_a_discarded_encode"),
    ("an incumbent-bound lane whose search has no incumbent arm",
     "UPDATE lane SET decision_rule = 'incumbent' WHERE lane = 'm4-ipad-le1080p-sdr'; "
     "UPDATE arm SET role = 'candidate', anchor_value = NULL WHERE arm_id = 'arm-i'",
     "x_incumbent_rule_without_incumbent_arm"),
    ("a target-bound lane whose search has no targets",
     "UPDATE lane SET decision_rule = 'target', score_target = 78.1 WHERE lane = 'm4-ipad-le1080p-sdr'; DELETE FROM search_target",
     "x_target_rule_without_targets"),
    ("a measured constant never calibrated", "DELETE FROM constant_value WHERE name = 'HEADROOM'",
     "x_measured_constant_never_calibrated"),
    ("an incumbent arm no viewing accepted", "UPDATE arm SET accepted_by_viewing = NULL WHERE arm_id = 'arm-i'",
     "x_incumbent_arm_not_viewed"),
    ("an incumbent arm accepted by a viewing of a different configuration",
     "INSERT INTO viewing_verdict VALUES (2, 'acceptance', 'm4-ipad-le1080p-sdr', 'tng', 'g-a', NULL, 'iPad M4 13in', 'andy', 'acceptable', NULL, '2026-09-04'); "
     "UPDATE arm SET accepted_by_viewing = 2 WHERE arm_id = 'arm-i'",
     "incumbent_viewing_matches_arm"),
    ("an incumbent arm with no scored cell at its pinned anchor on a member",
     "DELETE FROM score WHERE cell_key = 'c-i30-parks'; UPDATE encode SET kept = 1 WHERE cell_key = 'c-i30-parks'",
     "incumbent_arm_scored"),
    ("a target-bound lane whose targets name no viewing",
     "UPDATE lane SET decision_rule = 'target', score_target = 78.1 WHERE lane = 'm4-ipad-le1080p-sdr'",
     "x_target_without_a_viewing"),
    ("a search whose base arm was demoted", "UPDATE arm SET role = 'candidate' WHERE role = 'base'",
     "x_search_arm_roles"),
    ("a shipping arm from another search",
     "INSERT INTO search VALUES ('other', 'native-1080p-sdr', 'intel-b580-ihd26.2.2-qsv-av1', 'qsv.q', 1548, NULL, NULL); "
     "INSERT INTO arm VALUES ('arm-o', 'other', 'base', 'base', NULL, NULL); "
     "UPDATE search SET shipping_arm_id = 'arm-o' WHERE search_id = 'b580-qsv-av1'",
     "x_shipping_arm_not_in_search"),
    ("a locate cell off the coarse ladder", "UPDATE cell_setting SET value = '25' WHERE cell_key = 'l-a24-tng' AND setting_id = 'qsv.q'",
     "x_locate_cell_off_the_coarse_ladder"),
    ("a cell at an anchor past the encoder's range", "UPDATE cell_setting SET value = '55' WHERE cell_key = 'c-a50-tng' AND setting_id = 'qsv.q'",
     "x_cell_anchor_outside_range"),
    ("a rung of the codec ladder the shipping arm never encoded",
     "UPDATE cell_setting SET value = '23' WHERE cell_key = 'c-a24-tng' AND setting_id = 'qsv.q'",
     "shipping_arm_ladder_complete"),
    ("a lane floor the shipped rate is under", "UPDATE lane SET min_content_rate = 100 WHERE lane = 'm4-ipad-le1080p-sdr'",
     "content_rate_meets_floor"),
    ("a floor on a lane whose shipped row has no timing", "UPDATE lane SET min_content_rate = 1 WHERE lane = 'm4-ipad-gt1080p-sdr'",
     "content_rate_meets_floor"),
    ("a lane a unit supports with no route and no reason",
     "DELETE FROM routing_exclusion WHERE lane = 'kids-ipad-standard-sdr' AND host = 'media-01'",
     "x_supported_lane_not_routed"),
    ("a shipped unit that is not in that host", "UPDATE shipped SET host = 'eta' WHERE shipped_id = 3",
     "x_shipped_unit_not_on_host"),
    ("routed and excluded at once", "INSERT INTO routing_exclusion VALUES ('m4-ipad-le1080p-sdr', 'media-01', 'contradiction')",
     "x_routed_and_excluded"),
    ("a complete run with a cell still planned",
     "INSERT INTO cell VALUES ('c-planned', 'b580-qsv-av1', 'tng', 'reference'); "
     "INSERT INTO cell_setting VALUES ('c-planned', 'qsv.preset', '4', 'identity'), ('c-planned', 'qsv.b_strategy', '0', 'identity'), ('c-planned', 'qsv.q', '24', 'identity')",
     "x_complete_run_with_planned_cells"),
    ("a run whose state is not its latest event", "UPDATE run SET state = 'failed' WHERE run_id = 'b580-qsv-av1'",
     "x_run_state_disagrees_with_events"),
    ("two active runs on one host", "UPDATE run SET state = 'running' WHERE run_id IN ('b580-qsv-av1', 'b580-qsv-av1-time')",
     "x_two_active_runs_on_a_host"),
    ("a run on a blocked host", "UPDATE host SET blocked = 'the fix' WHERE host = 'media-01'",
     "x_run_on_a_blocked_host"),
    ("a run whose unit is not in its host", "UPDATE run SET host = 'eta' WHERE run_id = 'b580-qsv-av1-screen'",
     "x_run_unit_not_on_host"),
    ("a verdict with no encodes behind it", "DELETE FROM setting_verdict_cell WHERE verdict_id = 3",
     "x_verdict_without_cells"),
    ("an encode short of its cut's frames", "UPDATE encode SET frames = 1000 WHERE cell_key = 'c-a24-tng'",
     "x_encode_short_of_frames"),
    ("a screen encode short of its cut's frames -- the screen has no search to join through",
     "UPDATE encode SET frames = 1000 WHERE cell_key = 's-p1'",
     "x_encode_short_of_frames"),
    ("a cell with no rate-control mode among its settings", "DELETE FROM cell_setting WHERE cell_key = 's-p1' AND setting_id = 'qsv.q'",
     "x_cell_without_a_rate_mode"),
    ("a search scored at a height no served lane uses", "UPDATE search SET score_height = 1080 WHERE search_id = 'b580-qsv-av1'",
     "x_search_height_not_a_lane_height"),
    ("a score at another height", "UPDATE score SET height = 1080 WHERE cell_key = 'c-a24-tng' AND metric = 'vmaf'",
     "x_score_at_another_height"),
    ("a reference cut in use with no content check", "DELETE FROM cut_check WHERE cut_id = 'shield.ref'",
     "x_reference_cut_unchecked"),
    ("an encode discarded before it was scored", "DELETE FROM score WHERE cell_key = 'c-a24-tng'",
     "x_discarded_without_score"),
    ("locate arms with disjoint bitrate spans",
     "INSERT INTO cell VALUES ('l-b24-tng', 'b580-qsv-av1-locate', 'tng', 'reference'), ('l-b30-tng', 'b580-qsv-av1-locate', 'tng', 'reference'); "
     "INSERT INTO cell_setting VALUES ('l-b24-tng', 'qsv.preset', '4', 'identity'), ('l-b24-tng', 'qsv.b_strategy', '1', 'identity'), ('l-b24-tng', 'qsv.q', '24', 'identity'), "
     "('l-b30-tng', 'qsv.preset', '4', 'identity'), ('l-b30-tng', 'qsv.b_strategy', '1', 'identity'), ('l-b30-tng', 'qsv.q', '30', 'identity'); "
     "INSERT INTO encode VALUES ('l-b24-tng', 1000000, 200.0, 1439, 60.0, 'hardware', 0), ('l-b30-tng', 800000, 100.0, 1439, 60.0, 'hardware', 0)",
     "x_arms_with_disjoint_bitrate_spans"),
    ("a cell in a search run matching no arm",
     "INSERT INTO cell VALUES ('c-x', 'b580-qsv-av1', 'tng', 'reference'); "
     "INSERT INTO cell_setting VALUES ('c-x', 'qsv.preset', '1', 'identity'), ('c-x', 'qsv.q', '24', 'identity')",
     "cells_match_an_arm"),
]

DDL_REFUSALS = [
    ("a score with no height", "INSERT INTO score (cell_key, height, metric, statistic, value) VALUES ('c-a24-tng', NULL, 'vmaf', 'p5', 1.0)"),
    ("a shipped step the lane does not have", "INSERT INTO shipped (lane, host, step, provenance, decided_by) VALUES ('kids-ipad-standard-sdr','media-01','remux','derived','measurement')"),
    ("measured with no evidence class", "UPDATE shipped SET content_class_id = NULL WHERE shipped_id = 1"),
    ("a policy decision with no reason", "UPDATE shipped SET reason = NULL WHERE shipped_id = 4"),
    ("a classified cut check with no reason", "INSERT INTO cut_check VALUES ('tng.ref','dv_rpu','classified',NULL,'2026-08-26')"),
    ("a derived constant with no precision", "INSERT INTO constant VALUES ('X', 1.0, 'u', 'derived', '{}', NULL, NULL, NULL)"),
    ("a policy constant with no reason", "INSERT INTO constant VALUES ('Y', 0.5, 'u', 'policy', NULL, NULL, NULL, NULL)"),
    ("a policy constant with no value", "INSERT INTO constant VALUES ('Z', NULL, 'u', 'policy', NULL, NULL, 'because', NULL)"),
    ("an incumbent arm with no pinned anchor", "INSERT INTO arm VALUES ('arm-j', 'b580-qsv-av1', 'incumbent-2', 'incumbent', NULL, NULL)"),
    ("a base arm with a pinned anchor", "UPDATE arm SET anchor_value = '30' WHERE arm_id = 'arm-a'"),
    ("a second ladder for a codec", "INSERT INTO ladder VALUES ('av1-b','av1')"),
    ("a target-bound lane with no target", "UPDATE lane SET decision_rule = 'target' WHERE lane = 'kids-ipad-standard-sdr'"),
    ("a measured constant with a typed value", "UPDATE constant SET value = 20 WHERE name = 'BOUND'"),
    ("an acceptance viewing with no lane", "INSERT INTO viewing_verdict VALUES (3, 'acceptance', NULL, 'tng', 'g-a', NULL, 'd', 'v', 'acceptable', NULL, '2026-09-04')"),
    ("a pair viewing with one encode", "INSERT INTO viewing_verdict VALUES (3, 'pair', NULL, 'tng', 'g-a', NULL, 'd', 'v', 'same', NULL, '2026-09-04')"),
    ("a remux row that is not fixed", "UPDATE shipped SET provenance = 'derived' WHERE shipped_id = 6"),
    ("a fixed provenance on an encode step", "UPDATE shipped SET provenance = 'fixed' WHERE shipped_id = 3"),
    ("an EXCLUDED verdict with no reason", "INSERT INTO setting_verdict VALUES (9, 'intel-b580-ihd26.2.2-qsv-av1', 'qsv.tile_cols', 'tng', 'EXCLUDED', NULL, NULL, NULL, NULL, NULL)"),
    ("a device addressed by render node", "INSERT INTO host_unit VALUES ('htpc-01', 'nvidia-5060ti-595-nvenc-av1', '/dev/dri/renderD128')"),
    ("a failure with no stderr", "INSERT INTO cell_failure VALUES ('c-b24-tng', '2026-09-02', NULL, 1)"),
    ("a complete run never verified", "UPDATE run SET verified_at = NULL WHERE run_id = 'b580-qsv-av1'"),
    ("a cap-bound lane with no cap constant", "UPDATE lane SET bitrate_cap_binds = 'never', bitrate_cap_constant = NULL WHERE lane = 'm4-ipad-gt1080p-sdr'"),
    ("a host with no machine", "INSERT INTO host (host, ssh_host, os, work_root, ffmpeg) VALUES ('h', 'h', 'linux', '/w', '/f')"),
    ("a run with no artifact",
     "INSERT INTO run (run_id, encoder_unit_id, host, node_label, stage, ffmpeg_build, ffmpeg_sha, harness_version, started_at) "
     "VALUES ('r', 'intel-b580-ihd26.2.2-qsv-av1', 'media-01', 'media-01-b580', 'probe', 'b', 's', 'g0', '2026-09-05')"),
    ("an event by nobody", "INSERT INTO run_event (run_id, at, state) VALUES ('b580-qsv-av1', '2026-09-03T03:00', 'running')"),
]


def run_mutations(tags):
    """[(name, expected, fired: bool, other_fired: [..])]"""
    out = []
    for name, sql, expected in MUTATIONS:
        conn = load_schema()
        load_fixture(conn)
        conn.executescript(sql)
        res = run_checks(conn, tags)
        conn.close()
        fired = [k for k, v in res.items() if v]
        out.append((name, expected, expected in fired, [f for f in fired if f != expected]))
    return out


def run_ddl_refusals():
    out = []
    for name, sql in DDL_REFUSALS:
        conn = load_schema()
        load_fixture(conn)
        try:
            conn.executescript(sql)
            out.append((name, False))
        except sqlite3.IntegrityError:
            out.append((name, True))
        finally:
            conn.close()
    return out


# ---------------------------------------------------------------- render

def _wrap_columns(cols, width=70):
    lines, cur = [], ""
    for c in cols:
        piece = c if not cur else " · " + c
        if cur and len(cur) + len(piece) > width:
            lines.append(cur)
            cur = "· " + c
        else:
            cur += piece
    lines.append(cur)
    return lines


def render_listing(conn, tags, names):
    out = []
    for t in names:
        en = enums(conn, t)
        cols = [f"{c['name']} ∈ {{{', '.join(en[c['name']])}}}" if c["name"] in en else c["name"]
                for c in columns(conn, t)]
        lines = _wrap_columns(cols)
        tag = tags[t]["class"] + ("" if tags[t]["class"] == "FILE" else f" ({tags[t]['writer']})")
        first = f"    {t:<23}" + lines[0]
        rest = [f"    {'':<23}" + l for l in lines[1:]]
        all_lines = [first] + rest
        last = all_lines[-1]
        all_lines[-1] = (last.ljust(99) + tag) if len(last) < 98 else last + "\n" + " " * 99 + tag
        out.extend(all_lines)
    return "\n".join(out)


def render_diagram(conn, tags, group):
    names = [t for t in db_tables(conn) if tags[t]["group"] == group]
    lines = ["```mermaid", "erDiagram"]
    for t in names:
        pk = {c["name"] for c in columns(conn, t) if c["pk"]}
        for from_cols, to_table, to_cols in foreign_keys(conn, t):
            one_to_one = set(from_cols) == pk
            rel = "||--||" if one_to_one else "||--o{"
            lines.append(f"    {to_table} {rel} {t} : \"{','.join(from_cols)}\"")
    for t in names:
        en = enums(conn, t)
        pk = {c["name"] for c in columns(conn, t) if c["pk"]}
        fk = {c for from_cols, _, _ in foreign_keys(conn, t) for c in from_cols}
        lines.append(f"    {t} {{")
        for c in columns(conn, t):
            keys = [k for k, on in (("PK", c["name"] in pk), ("FK", c["name"] in fk)) if on]
            comment = f' "{" | ".join(en[c["name"]])}"' if c["name"] in en else ""
            lines.append(f"        {c['type'] or 'ANY'} {c['name']}{(' ' + ', '.join(keys)) if keys else ''}{comment}")
        lines.append("    }")
    lines.append("```")
    return "\n".join(lines)


def render_fk_list(conn):
    out = []
    for t in db_tables(conn):
        for from_cols, to_table, to_cols in foreign_keys(conn, t):
            out.append(f"    {t}.{','.join(from_cols)} -> {to_table}.{','.join(to_cols)}")
    return "\n".join(out)


def render_writers(conn, tags):
    by_writer = OrderedDict()
    for t in db_tables(conn):
        by_writer.setdefault(tags[t]["writer"], []).append(t)
    order = ["inventory", "materialise", "verify", "orchestrate", "encode core", "derive ladders", "screen", "score",
             "time", "calibrate", "ship", "viewing", "authored"]
    out = []
    for w in order + [w for w in by_writer if w not in order]:
        if w not in by_writer:
            continue
        lines = _wrap_columns(by_writer[w], width=76)
        out.append(f"    {w:<13} ->  " + lines[0])
        out.extend(f"    {'':<13}     " + l for l in lines[1:])
    return "\n".join(out)


def render_checks(conn, checks):
    rows = ["| check | refuses | fix |", "|---|---|---|"]
    for v in db_views(conn, "x_"):
        c = checks.get(v, {})
        rows.append(f"| `{v}` | {c.get('check', '')} | {c.get('fix', '')} |")
    for k, (refuses, fix) in SCRIPT_CHECKS.items():
        rows.append(f"| `{k}` (script) | {refuses} | {fix} |")
    rows.append("")
    rows.append("**Enforced by the DDL itself, so no view is needed:** " + " · ".join(DDL_ENFORCED) + ".")
    return "\n".join(rows)


def render_regions(conn, tags, checks):
    by_group = {g: [t for t in db_tables(conn) if tags[t]["group"] == g] for g in GROUPS}
    regions = OrderedDict()
    regions["sample:a"] = render_listing(conn, tags, ["window", "content_class", "content_class_lane",
                                                      "content_class_stratum", "content_class_member"])
    regions["sample:b"] = render_listing(conn, tags, ["reference_set", "cut", "cut_check"])
    for g in GROUPS:
        regions[f"schema:{g}"] = render_listing(conn, tags, by_group[g])
    regions["diagrams"] = "\n\n".join(f"**{g}**\n\n" + render_diagram(conn, tags, g) for g in GROUPS) \
        + "\n\nEvery foreign key, child to parent:\n\n" + render_fk_list(conn)
    regions["writers"] = render_writers(conn, tags)
    regions["checks"] = render_checks(conn, checks)
    return regions


def apply_regions(text, regions):
    """Replace each marked region; return (new_text, missing_markers, stale_names)."""
    missing, stale = [], []
    for name, body in regions.items():
        b, e = BEGIN.format(name), END.format(name)
        if text.count(b) != 1 or text.count(e) != 1:
            missing.append(name)
            continue
        i, j = text.index(b) + len(b), text.index(e)
        current = text[i:j].strip("\n")
        if current != body:
            stale.append(name)
        text = text[:i] + "\n" + body + "\n" + text[j:]
    return text, missing, stale


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--mutate", action="store_true")
    args = ap.parse_args(argv)

    conn = load_schema()
    tags, checks = parse_tags()
    tag_problems = check_tags(conn, tags)
    if tag_problems:
        print("REFUSING: tags\n  " + "\n  ".join(tag_problems))
        return 1

    if args.render or args.check:
        regions = render_regions(conn, tags, checks)
        text = DOC.read_text()
        new, missing, stale = apply_regions(text, regions)
        if missing:
            print("REFUSING: DATA-MODEL.md lacks markers for: " + ", ".join(missing))
            return 1
        if args.check:
            if stale:
                print("STALE regions in DATA-MODEL.md: " + ", ".join(stale) + " -- run --render")
                return 1
            print("DATA-MODEL.md generated regions: current")
            return 0
        DOC.write_text(new)
        print("rendered " + ", ".join(regions) + (f" (changed: {', '.join(stale)})" if stale else " (no change)"))
        return 0

    load_fixture(conn)
    results = run_checks(conn, tags)
    bad = {k: v for k, v in results.items() if v}
    for k in results:
        print(f"  {'FAIL' if k in bad else 'ok  '} {k}" + (f"  {bad[k][:3]}" if k in bad else ""))
    rc = 1 if bad else 0

    if args.mutate:
        print("negative cases:")
        for name, expected, fired, others in run_mutations(tags):
            print(f"  {'CAUGHT ' if fired else 'MISSED '} {name} -> {expected}" + (f"  (also: {', '.join(others)})" if others else ""))
            rc |= 0 if fired else 1
        print("DDL refusals:")
        for name, refused in run_ddl_refusals():
            print(f"  {'REFUSED' if refused else 'ALLOWED'} {name}")
            rc |= 0 if refused else 1
    print("model_check:", "OK" if rc == 0 else "FAILED")
    return rc


if __name__ == "__main__":
    sys.exit(main())
