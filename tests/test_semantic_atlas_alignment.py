from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from brain.atlas import build_atlas
from brain.catalog import (
    _content_hash,
    collect_generation_components,
    current_generation_ref,
    publish_generation,
)
from brain.core import load_settings, snapshot_indexes
from brain.editions import capabilities
from brain.ops import refresh_brain, semantic_status
from brain.semantic import (
    ATLAS_CARD_VERSION,
    CARD_VERSION,
    CHUNK_SCHEMA_VERSION,
    SEMANTIC_EMBEDDING_INPUT_VERSION,
    SEMANTIC_MAX_CARD_INPUT_BYTES,
    build_semantic_index,
    semantic_schema_version,
    semantic_state_compatibility,
)


class SemanticAtlasAlignmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        repository = self.root / "service"
        repository.mkdir()
        (repository / "service.py").write_text(
            "def stable_feature():\n    return 'stable'\n",
            encoding="utf-8",
        )
        config = self.root / "brain.toml"
        config.write_text(
            "[project]\nname='semantic-atlas-alignment'\n"
            "[graph]\nenabled=false\n[experience]\nenabled=false\n"
            "[[repositories]]\nname='service'\npath='service'\n",
            encoding="utf-8",
        )
        self.settings = load_settings(config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def embed(cards: list[str]) -> list[list[float]]:
        return [[1.0, float(len(card) % 17), 0.5] for card in cards]

    def atlas_state(self, sha: str = "source-S") -> tuple[dict[str, object], dict[str, object]]:
        repo = self.settings.repo("service")
        repo.source_sha = sha
        repo.source_ref = "refs/heads/main"
        state, _ = snapshot_indexes(self.settings, changed_only=False, publish=False)
        return state, build_atlas(self.settings, state)

    def publish(self, state: dict[str, object], atlas: dict[str, object], *, semantic_failed: bool = False):
        components = collect_generation_components(
            self.settings, state, semantic_failed=semantic_failed, atlas_payload=atlas,
        )
        publish_generation(self.settings, state, components=components, atlas_payload=atlas)
        generation = current_generation_ref(self.settings)
        self.assertIsNotNone(generation)
        return generation

    def test_compatible_semantic_state_is_reused_and_registered_for_current_atlas(self) -> None:
        state, atlas = self.atlas_state()
        core_generation = self.publish(state, atlas, semantic_failed=True)
        self.assertEqual("unavailable", core_generation.component("semantic")["status"])

        build_semantic_index(
            self.settings, embed=self.embed, pack_id="upgrade-pack", atlas_cards=atlas["cards"],
        )
        before = (self.settings.state_dir / "semantic-index.json").read_bytes()

        def unexpected_embed(_cards: list[str]) -> list[list[float]]:
            raise AssertionError("a compatible Semantic generation must not be re-embedded")

        reused = build_semantic_index(
            self.settings, embed=unexpected_embed, pack_id="upgrade-pack", atlas_cards=atlas["cards"],
        )
        self.assertGreater(reused["chunks"], 0)
        self.assertEqual(before, (self.settings.state_dir / "semantic-index.json").read_bytes())

        generation = self.publish(state, atlas)
        self.assertNotEqual(core_generation.generation, generation.generation)
        self.assertEqual("ready", generation.component("semantic")["status"])
        self.assertTrue(semantic_status(self.settings)["aligned"])
        self.assertFalse(semantic_status(self.settings)["stale"])
        self.assertTrue(capabilities(self.settings)["semantic_aligned"])

    def test_incompatible_v08_state_rebuilds_with_cache_reuse_and_preserves_old_component(self) -> None:
        state, atlas = self.atlas_state()
        progress: list[dict[str, object]] = []
        build_semantic_index(
            self.settings, embed=self.embed, pack_id="upgrade-pack", atlas_cards=[],
        )
        semantic_path = self.settings.state_dir / "semantic-index.json"
        old_state = json.loads(semantic_path.read_text(encoding="utf-8"))
        old_state.pop("atlas_card_version")
        semantic_path.write_text(json.dumps(old_state), encoding="utf-8")

        old_components = collect_generation_components(
            self.settings, state, semantic_failed=True, atlas_payload=atlas,
        )
        old_components["semantic"] = {
            "schema_version": f"{CHUNK_SCHEMA_VERSION}:{CARD_VERSION}:{SEMANTIC_EMBEDDING_INPUT_VERSION}",
            "status": "ready",
            "content_hash": _content_hash(old_state),
            "details": {
                "pack_id": old_state["pack_id"], "dimension": old_state["dimension"],
                "backend": old_state["backend"], "snapshots": old_state["snapshots"],
            },
            "_artifact_source": str(semantic_path),
        }
        publish_generation(self.settings, state, components=old_components, atlas_payload=atlas)
        generation_one = current_generation_ref(self.settings)
        old_artifact = self.settings.state_dir / str(generation_one.component("semantic")["artifact_ref"])
        self.assertTrue(old_artifact.is_file())
        self.assertFalse(semantic_status(self.settings)["aligned"])

        build_semantic_index(
            self.settings,
            embed=self.embed,
            pack_id="upgrade-pack",
            atlas_cards=atlas["cards"],
            progress=progress.append,
        )
        generation_two = self.publish(state, atlas)
        self.assertNotEqual(generation_one.generation, generation_two.generation)
        self.assertTrue(old_artifact.is_file())
        self.assertEqual("ready", generation_two.component("semantic")["status"])
        self.assertTrue(semantic_status(self.settings)["aligned"])
        self.assertGreater(max(int(item.get("cached_embeddings_reused") or 0) for item in progress), 0)
        self.assertGreater(max(int(item.get("new_embeddings_completed") or 0) for item in progress), 0)

    def test_large_atlas_card_is_bounded_without_rejecting_the_refresh(self) -> None:
        state, _ = self.atlas_state()
        cards_seen: list[str] = []
        oversized = {
            "repo": "service",
            "level": "module",
            "target_id": "sha256:module",
            "card_id": "sha256:card",
            "content_hash": "sha256:content",
            "path": "service.py",
            "content": "Module service:service.py\nEntities: " + ("very_long_entity_name, " * 900),
            "metadata": {},
        }

        def bounded_embed(cards: list[str]) -> list[list[float]]:
            cards_seen.extend(cards)
            self.assertTrue(all(len(card.encode("utf-8")) <= SEMANTIC_MAX_CARD_INPUT_BYTES for card in cards))
            return self.embed(cards)

        built = build_semantic_index(
            self.settings, embed=bounded_embed, pack_id="bounded-pack", atlas_cards=[oversized],
        )
        self.assertGreater(built["chunks"], 0)
        self.assertTrue(any("[semantic card code truncated]" in card for card in cards_seen))
        semantic = json.loads((self.settings.state_dir / "semantic-index.json").read_text(encoding="utf-8"))
        self.assertEqual(ATLAS_CARD_VERSION, semantic["atlas_card_version"])
        self.assertEqual({"service": "source-S"}, semantic["snapshots"])

    def test_precision_refresh_reports_ready_only_after_atlas_registration(self) -> None:
        original = build_semantic_index

        def injected(settings, *, progress=None):
            return original(settings, embed=self.embed, pack_id="precision-pack", progress=progress)

        with (
            mock.patch("brain.editions.current_edition", return_value="precision"),
            mock.patch("brain.semantic.build_semantic_index", side_effect=injected),
        ):
            outcome = refresh_brain(self.settings, fetch=False, discover=False)
        self.assertEqual("ready", outcome.semantic["status"])
        self.assertTrue(outcome.semantic["aligned"])
        self.assertFalse(outcome.semantic["stale"])
        self.assertTrue(outcome.semantic["atlas_core_published"])
        self.assertTrue(outcome.semantic["precision_ready"])
        self.assertEqual("ready", current_generation_ref(self.settings).component("semantic")["status"])

        events: list[dict[str, object]] = []
        with (
            mock.patch("brain.editions.current_edition", return_value="precision"),
            mock.patch("brain.semantic.build_semantic_index", side_effect=RuntimeError("synthetic failure")),
        ):
            failed = refresh_brain(self.settings, fetch=False, discover=False, progress=events.append)
        self.assertEqual("failed", failed.semantic["status"])
        self.assertFalse(failed.semantic["aligned"])
        self.assertTrue(failed.semantic["atlas_core_published"])
        self.assertFalse(failed.semantic["precision_ready"])
        self.assertEqual("unavailable", current_generation_ref(self.settings).component("semantic")["status"])
        self.assertEqual("Core refresh complete; Semantic needs attention", events[-1]["phase_label"])

    def test_alignment_rejects_component_and_shard_mismatches(self) -> None:
        state, atlas = self.atlas_state()
        build_semantic_index(
            self.settings, embed=self.embed, pack_id="validation-pack", atlas_cards=atlas["cards"],
        )
        generation = self.publish(state, atlas)
        component = generation.component("semantic")
        artifact = self.settings.state_dir / str(component["artifact_ref"])
        semantic = json.loads(artifact.read_text(encoding="utf-8"))
        snapshots = generation.snapshots

        wrong_source = json.loads(json.dumps(semantic))
        wrong_source["snapshots"]["service"] = "wrong-source"
        self.assertFalse(semantic_state_compatibility(self.settings, wrong_source, snapshots)[0])

        for field, value in (
            ("pack_id", "wrong-pack"),
            ("dimension", 99),
            ("source_signature", "sha256:wrong"),
        ):
            wrong_component = json.loads(json.dumps(component))
            wrong_component["details"][field] = value
            self.assertFalse(semantic_state_compatibility(
                self.settings, semantic, snapshots, component=wrong_component,
            )[0])

        wrong_hash = json.loads(json.dumps(component))
        wrong_hash["content_hash"] = "sha256:wrong"
        self.assertFalse(semantic_state_compatibility(
            self.settings, semantic, snapshots, component=wrong_hash,
        )[0])

        shard = self.settings.state_dir / "semantic-shards" / "fixture.usearch"
        shard.parent.mkdir(parents=True, exist_ok=True)
        shard.write_bytes(b"fixture")
        usearch_state = {
            "generation": "fixture",
            "chunk_schema_version": CHUNK_SCHEMA_VERSION,
            "card_version": CARD_VERSION,
            "embedding_input_version": SEMANTIC_EMBEDDING_INPUT_VERSION,
            "atlas_card_version": ATLAS_CARD_VERSION,
            "backend": "usearch", "pack_id": "production-pack", "dimension": 3, "stale": False,
            "snapshots": snapshots, "entries": [],
            "shards": [{
                "repo": "service", "snapshot": "source-S", "path": str(shard),
                "artifact_ref": shard.name, "artifact_bytes": shard.stat().st_size, "entries": [],
            }],
        }
        manifest = {"pack_id": "production-pack", "embedding_dimension": 3}
        with mock.patch("brain.semantic.active_pack", return_value=manifest):
            self.assertTrue(semantic_state_compatibility(self.settings, usearch_state, snapshots)[0])
            missing = json.loads(json.dumps(usearch_state))
            missing["shards"][0]["path"] = str(shard.with_name("missing.usearch"))
            self.assertFalse(semantic_state_compatibility(self.settings, missing, snapshots)[0])
            corrupt = json.loads(json.dumps(usearch_state))
            corrupt["shards"][0]["entries"] = {"not": "a list"}
            self.assertFalse(semantic_state_compatibility(self.settings, corrupt, snapshots)[0])
            wrong_pack = json.loads(json.dumps(usearch_state))
            wrong_pack["pack_id"] = "wrong-pack"
            self.assertFalse(semantic_state_compatibility(self.settings, wrong_pack, snapshots)[0])
            wrong_dimension = json.loads(json.dumps(usearch_state))
            wrong_dimension["dimension"] = 4
            self.assertFalse(semantic_state_compatibility(self.settings, wrong_dimension, snapshots)[0])

        live_projection = self.settings.state_dir / "semantic-index.json"
        stale = json.loads(live_projection.read_text(encoding="utf-8"))
        stale["stale"] = True
        live_projection.write_text(json.dumps(stale), encoding="utf-8")
        self.assertTrue(semantic_status(self.settings)["aligned"])
        self.assertEqual(
            "unavailable",
            collect_generation_components(self.settings, state, atlas_payload=atlas)["semantic"]["status"],
        )

        connection = sqlite3.connect(self.settings.state_dir / "catalog.sqlite3")
        try:
            connection.execute(
                "DELETE FROM generation_components WHERE generation=? AND component='semantic'",
                (generation.generation,),
            )
            connection.commit()
        finally:
            connection.close()
        self.assertFalse(semantic_status(self.settings)["aligned"])

    def test_atlas_publication_failure_keeps_previous_generation_current(self) -> None:
        original = build_semantic_index

        def injected(settings, *, progress=None):
            return original(settings, embed=self.embed, pack_id="rollback-pack", progress=progress)

        with (
            mock.patch("brain.editions.current_edition", return_value="semantic"),
            mock.patch("brain.semantic.build_semantic_index", side_effect=injected),
        ):
            refresh_brain(self.settings, fetch=False, discover=False)
        previous = current_generation_ref(self.settings)
        with (
            mock.patch("brain.editions.current_edition", return_value="semantic"),
            mock.patch("brain.semantic.build_semantic_index", side_effect=injected),
            mock.patch("brain.catalog.publish_generation", side_effect=sqlite3.OperationalError("publish failed")),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                refresh_brain(self.settings, fetch=False, discover=False)
        self.assertEqual(previous.generation, current_generation_ref(self.settings).generation)
        self.assertTrue(semantic_status(self.settings)["aligned"])

    def test_schema_version_is_explicitly_part_of_component_contract(self) -> None:
        self.assertEqual(
            f"{CHUNK_SCHEMA_VERSION}:{CARD_VERSION}:{SEMANTIC_EMBEDDING_INPUT_VERSION}:{ATLAS_CARD_VERSION}",
            semantic_schema_version(),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
