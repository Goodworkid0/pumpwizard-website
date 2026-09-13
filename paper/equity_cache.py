"""Public-worker-only incremental chart samples. No simulation state or writes."""
import time


class EquityCache:
    def __init__(self): self.series={}

    def rows(self, runner, bucket, now, minimum_at=None):
        key=(str(runner.path),bucket)
        # Never infer the required history from a strategy's reported age. Widen
        # a cached range when another chart using the same bucket needs more.
        floor=minimum_at-86400 if minimum_at is not None else None
        maximum=runner.db.execute('SELECT coalesce(max(id),0) FROM equity').fetchone()[0]
        saved=self.series.get(key)
        widen=saved is not None and saved['floor'] is not None and (floor is None or floor<saved['floor'])
        if saved is None or maximum<saved['last_id'] or widen:
            where='WHERE at>=? ' if floor is not None else ''
            parameters=(floor,bucket) if floor is not None else (bucket,)
            rows=runner.db.execute('SELECT e.id,e.at,e.value FROM equity e JOIN '
                '(SELECT max(id) AS id FROM equity '+where+'GROUP BY cast(at / ? AS integer)) sampled '
                'ON e.id=sampled.id ORDER BY e.id',parameters).fetchall()
            points={int(r['at']/bucket):dict(r) for r in rows}
        else:
            rows=runner.db.execute('SELECT id,at,value FROM equity WHERE id>? AND id<=? ORDER BY id LIMIT 50001',
                                   (saved['last_id'],maximum)).fetchall()
            if len(rows)>50000:
                del self.series[key]
                return self.rows(runner,bucket,now,minimum_at)
            points=dict(saved['points'])
            for row in rows: points[int(row['at']/bucket)]=dict(row)
            if saved['floor'] is None: floor=None
        if floor is not None: points={b:r for b,r in points.items() if r['at']>=floor}
        self.series[key]={'last_id':maximum,'points':points,'floor':floor}
        return sorted(points.values(),key=lambda r:r['id'])
