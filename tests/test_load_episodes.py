from kodik_pipeline.load import upsert_episode


class FakeCursor:
    def __init__(self):
        self.queries = []
        self.rows = {}
        self.next_id = 1
        self.pending = None

    def execute(self, sql, params=None):
        params = params or {}
        self.queries.append((sql, params))
        if sql.startswith("SELECT id FROM episodes"):
            self.pending = self.rows.get((params["season_id"], params["episode_number"]))
        elif sql.startswith("INSERT INTO episodes"):
            key = (params["season_id"], params["episode_number"])
            self.rows[key] = self.next_id
            self.next_id += 1

    def fetchone(self):
        row = self.pending
        self.pending = None
        return (row,) if row else None


def test_upsert_episode_creates_and_updates():
    cur = FakeCursor()
    assert upsert_episode(cur, 10, 1, "//x/1") is True
    assert upsert_episode(cur, 10, 1, "//x/2") is False
    assert cur.queries[-1][1]["player_link"] == "//x/2"
