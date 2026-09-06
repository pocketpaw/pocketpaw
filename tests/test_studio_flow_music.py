# tests/test_studio_flow_music.py — the music node + the movie pipeline.
#
# Created 2026-09-04 (studio-movie-pipeline).
#
# `build_studio_flow` is the agent's only way onto the Flow canvas, and it
# validates against a fixed kind list. A music node the agent emits but the
# validator does not know is rejected wholesale — the whole graph fails, not just
# that node — so the kind list and the description have to move together.
#
# The description matters as much as the schema here: the agent chooses the
# pipeline shape from prose, and a movie pipeline that omits the character-first
# ordering produces a video whose subject changes between the still and the clip.

from __future__ import annotations

from pocketpaw.tools.builtin.studio_flow_tool import (
    STUDIO_FLOW_DESCRIPTION,
    STUDIO_FLOW_KINDS,
    studio_flow_parameters,
    validate_flow_spec,
)


class TestMusicKind:
    def test_music_is_a_known_kind(self) -> None:
        """Without this the validator rejects the whole graph, not just the node."""
        assert "music" in STUDIO_FLOW_KINDS

    def test_the_json_schema_advertises_it(self) -> None:
        """The tool's enum is what the model actually sees. A kind that is valid
        internally but missing from the schema is one the agent never emits."""
        props = studio_flow_parameters()["properties"]
        enum = props["nodes"]["items"]["properties"]["type"]["enum"]
        assert "music" in enum

    def test_the_kind_list_and_the_schema_cannot_drift(self) -> None:
        props = studio_flow_parameters()["properties"]
        enum = props["nodes"]["items"]["properties"]["type"]["enum"]
        assert enum == list(STUDIO_FLOW_KINDS)


class TestMoviePipelineValidates:
    def _movie_graph(self) -> tuple[list[dict], list[dict]]:
        """The shape the description tells the agent to build."""
        nodes = [
            {
                "id": "m1",
                "type": "model",
                "position": {"x": 0, "y": 0},
                "data": {
                    "imageModel": "fal-ai/flux/dev",
                    "videoModel": "bytedance/seedance-2.0/reference-to-video",
                    "musicModel": "elevenlabs_music",
                },
            },
            {
                "id": "tc",
                "type": "text",
                "position": {"x": 320, "y": -160},
                "data": {"text": "a weathered detective in a wet trench coat"},
            },
            {
                "id": "ic",
                "type": "image",
                "position": {"x": 640, "y": -160},
                "data": {"prompt": "portrait of a weathered detective"},
            },
            {
                "id": "ts",
                "type": "text",
                "position": {"x": 320, "y": 0},
                "data": {"text": "he steps into a rain-slicked alley at night"},
            },
            {
                "id": "tm",
                "type": "text",
                "position": {"x": 320, "y": 160},
                "data": {"text": "slow melancholic piano, sparse, minor key"},
            },
            {
                "id": "mu",
                "type": "music",
                "position": {"x": 640, "y": 160},
                "data": {"prompt": "slow melancholic piano", "durationSec": 10},
            },
            {
                "id": "v1",
                "type": "video",
                "position": {"x": 960, "y": 0},
                "data": {"prompt": "@Image1 walks into the alley, scored by @Audio1"},
            },
            {"id": "o1", "type": "output", "position": {"x": 1280, "y": 0}, "data": {}},
        ]
        edges = [
            {"source": "m1", "target": "tc"},
            {"source": "tc", "target": "ic"},
            {"source": "ts", "target": "v1"},
            {"source": "tm", "target": "mu"},
            {"source": "ic", "target": "v1"},
            {"source": "mu", "target": "v1"},
            {"source": "v1", "target": "o1"},
        ]
        return nodes, edges

    def test_the_full_movie_graph_validates(self) -> None:
        nodes, edges = self._movie_graph()
        spec, err = validate_flow_spec(nodes, edges, goal="a noir short", flow_id="f1")
        assert err is None, err
        assert {n["type"] for n in spec["nodes"]} >= {"model", "image", "music", "video"}

    def test_both_the_image_and_the_music_reach_the_video_node(self) -> None:
        """The point of the whole pipeline: one video call conditioned on the
        character still AND the generated track."""
        nodes, edges = self._movie_graph()
        spec, _ = validate_flow_spec(nodes, edges, goal="g", flow_id="f1")
        into_video = {e["source"] for e in spec["edges"] if e["target"] == "v1"}
        assert "ic" in into_video
        assert "mu" in into_video


class TestDescriptionTeachesThePipeline:
    """The agent picks the graph shape from this prose, so the load-bearing facts
    have to be IN it. These are cheap and they catch a description edited down to
    the point where the agent stops building the right thing."""

    def test_it_names_the_music_node(self) -> None:
        assert "music" in STUDIO_FLOW_DESCRIPTION

    def test_it_says_the_character_is_generated_first(self) -> None:
        # Ordering is the part that goes wrong invisibly: a video conditioned on
        # no character still produces a different person than the image did.
        assert "CHARACTER first" in STUDIO_FLOW_DESCRIPTION

    def test_it_tells_the_agent_to_cite_the_references(self) -> None:
        # An uncited reference is ignored by the model and nothing reports it.
        assert "@Image1" in STUDIO_FLOW_DESCRIPTION
        assert "@Audio1" in STUDIO_FLOW_DESCRIPTION

    def test_it_names_all_three_models_on_the_model_node(self) -> None:
        for field in ("imageModel", "videoModel", "musicModel"):
            assert field in STUDIO_FLOW_DESCRIPTION

    def test_it_says_the_prompts_are_user_editable_before_running(self) -> None:
        # The agent writes finished prompts precisely because the user edits them
        # on the canvas rather than the graph running on emit.
        assert "Run all" in STUDIO_FLOW_DESCRIPTION
