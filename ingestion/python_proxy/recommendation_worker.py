from __future__ import annotations

import sys
import time
import traceback

import server


def main() -> int:
    print("Auralis Recommendation Worker")
    print(
        "scheduler="
        f"{'enabled' if server.RECOMMENDATION_ENABLE_SCHEDULER else 'disabled'} "
        f"external={'1' if server.RECOMMENDATION_EXTERNAL_WORKER else '0'} "
        f"sync={server.RECOMMENDATION_SYNC_INTERVAL_SECONDS}s "
        f"train={server.RECOMMENDATION_TRAIN_INTERVAL_SECONDS}s "
        f"eval={server.RECOMMENDATION_EXPERIMENT_EVAL_INTERVAL_SECONDS}s"
    )
    try:
        server.run_recommendation_worker_forever()
    except KeyboardInterrupt:
        print("Recommendation worker stopped")
        return 0
    except Exception:
        traceback.print_exc()
        time.sleep(1)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
