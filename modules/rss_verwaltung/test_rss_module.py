import json
import os
import tempfile
import unittest

from modules.rss_verwaltung import module as rss


def reset_db(path):
    conn = getattr(rss._db_local, "conn", None)
    if conn is not None:
        conn.close()
    rss._db_local.conn = None
    rss.DB_FILE = path
    rss.OLD_JSON = os.path.join(os.path.dirname(path), "quellen.json")
    return rss._init_db()


class RssRagIngestTests(unittest.TestCase):
    def test_ingest_rag_stores_note_and_deduplicates_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = reset_db(os.path.join(tmp, "rss.sqlite3"))
            now = "2026-05-09T10:00:00Z"
            conn.execute(
                """
                INSERT INTO sources(
                    id,url,name,category,language,reliability,alignment,reach,
                    freshness_hint,tags_json,notes,active,created_at_utc,updated_at_utc
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    "src1",
                    "https://example.test/feed.xml",
                    "Example News",
                    "Politik",
                    "de",
                    "hoch",
                    "neutral",
                    "national",
                    "taeglich",
                    json.dumps(["energie", "politik"]),
                    "",
                    1,
                    now,
                    now,
                ],
            )
            conn.execute(
                """
                INSERT INTO items(
                    id,source_id,guid,url,title,summary,published_at_utc,
                    fetched_at_utc,content_hash,tags_json,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    "item1",
                    "src1",
                    "guid1",
                    "https://example.test/news/1",
                    "Energie Politik Beschluss",
                    "Die Regierung beschliesst neue Energie Regeln.",
                    now,
                    now,
                    "hash1",
                    "[]",
                    "{}",
                ],
            )
            conn.commit()

            config = {
                "data_dir": os.path.join(tmp, "data"),
                "rag_pool": "News",
                "rss_db_path": os.path.join(tmp, "rss.sqlite3"),
            }
            result = rss._ingest_rag(json.dumps({"query": "energie politik", "limit": 10}), config)
            self.assertIn("stored: 1", result)

            search = rss._suche(json.dumps({"query": "Energiepolitik Deutschland", "limit": 5}), config)
            self.assertIn("results: 1", search)
            self.assertIn("Energie Politik Beschluss", search)

            rag_dir = os.path.join(config["data_dir"], "rag", "News")
            files = os.listdir(rag_dir)
            self.assertEqual(len(files), 1)
            with open(os.path.join(rag_dir, files[0]), encoding="utf-8") as fh:
                entry = json.load(fh)
            self.assertTrue(entry["text"].startswith("RSS_NEWS_NOTE"))
            self.assertIn("source_reliability: hoch", entry["text"])
            self.assertIn("source_alignment: neutral", entry["text"])

            second = rss._ingest_rag(json.dumps({"query": "energie politik", "limit": 10}), config)
            self.assertIn("stored: 0", second)
            self.assertEqual(len(os.listdir(rag_dir)), 1)

            conn.execute(
                """
                INSERT INTO items(
                    id,source_id,guid,url,title,summary,published_at_utc,
                    fetched_at_utc,content_hash,tags_json,raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    "item2",
                    "src1",
                    "guid2",
                    "https://example.test/news/1",
                    "Energie Politik Beschluss",
                    "Dubletten aus einem zweiten Feed.",
                    now,
                    now,
                    "hash2",
                    "[]",
                    "{}",
                ],
            )
            conn.commit()

            dupe = rss._ingest_rag(json.dumps({"query": "energie politik", "limit": 10}), config)
            self.assertIn("linked_duplicates: 1", dupe)
            self.assertEqual(len(os.listdir(rag_dir)), 1)


if __name__ == "__main__":
    unittest.main()
