from __future__ import annotations

import json

from scripts import create_ciao_che_torna_episode as base


SHORT_CONTINUITY_LOCK = (
    "Continuity: one identical Emma in the same pink-dress look and the same sunny square. "
    "Keep every recurring passer-by, outfit, prop and position consistent. No duplicate Emma, "
    "fantasy creatures, transformations, random objects, extra animals, crowds, text or logos. "
    "Anyone Emma greets must visibly look at her and return the greeting. "
)


def _compact_storyboard_actions() -> None:
    for scene in base.SCENES:
        current = str(scene.get("action") or "")
        if current.startswith(base.CONTINUITY_LOCK):
            specific_action = current[len(base.CONTINUITY_LOCK):]
        else:
            specific_action = current
        compact = SHORT_CONTINUITY_LOCK + specific_action
        if len(compact) > 800:
            raise RuntimeError(
                f"Scene {scene.get('index')} compact action is still {len(compact)} characters"
            )
        scene["action"] = compact


def main() -> None:
    _compact_storyboard_actions()
    result = base.upsert_episode()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
