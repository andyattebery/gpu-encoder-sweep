"""sweep/node/pool.py -- concurrency handled once: every future is joined, and the first error in submission order is raised."""
from concurrent.futures import ThreadPoolExecutor


def run_all(jobs, workers):
    """The results of every job, in order; a failure is raised after every job has finished, the earliest submitted first."""
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(job) for job in jobs]
        results, first = [], None
        for f in futures:
            try:
                results.append(f.result())
            except Exception as e:      # noqa: BLE001 -- joined, then re-raised in order
                results.append(None)
                first = first or e
        if first is not None:
            raise first
        return results
