"""Synthetic couch scenes, in normalized coordinates.

The cushion is a seat band across the frame. A floor dog's feet sit below it,
as they do from an elevated camera, where the couch's front face separates seat
from floor. These are geometric cases, not evidence of real-world accuracy.
"""

import pytest
from conftest import dog, person

from acacia.classifier import LABEL_AMBIGUOUS, LABEL_OFF, LABEL_ON, LABEL_WEAK, classify_frame, score_dog
from acacia.config import ClassifierConfig
from acacia.geometry import CushionMask, Polygon
from acacia.state import Evidence

SEAT = [[0.2, 0.40], [0.9, 0.40], [0.9, 0.62], [0.2, 0.62]]
CFG = ClassifierConfig()
DOG_CONF = 0.40


@pytest.fixture
def mask():
    return CushionMask([Polygon("a", "seat", True, SEAT)], 1280, 720)


def classify(mask, dogs=(), people=()):
    return classify_frame(list(dogs), list(people), mask, CFG, DOG_CONF)


def test_empty_couch(mask):
    assert classify(mask).evidence is Evidence.CLEAR_EMPTY


def test_dog_sitting_on_a_cushion(mask):
    result = classify(mask, [dog((0.45, 0.25, 0.55, 0.58))])
    assert result.evidence is Evidence.ON
    assert result.dogs[0].foot_in


def test_dog_lying_along_a_cushion_has_no_footpoint_trouble(mask):
    result = classify(mask, [dog((0.35, 0.44, 0.70, 0.58))])
    assert result.evidence is Evidence.ON


def test_floor_dog_far_from_the_couch(mask):
    result = classify(mask, [dog((0.30, 0.75, 0.42, 0.95))])
    assert result.evidence is Evidence.CLEAR_SEEN
    assert result.dogs[0].label == LABEL_OFF


def test_floor_dog_whose_box_crosses_the_cushions_is_not_on(mask):
    """Perspective case: most of the box overlaps cushions, but the feet are on the floor."""
    tall_box = (0.40, 0.42, 0.55, 0.72)
    result = classify(mask, [dog(tall_box)])
    assert result.dogs[0].box_overlap > 0.6  # the old 60 percent rule would have said ON
    assert result.evidence is not Evidence.ON
    assert not result.dogs[0].foot_in


def test_paws_only_is_not_on(mask):
    """Agreed ground truth: front paws on the couch with hind legs on the floor is not ON."""
    result = classify(mask, [dog((0.40, 0.45, 0.52, 0.80))])
    assert result.evidence is Evidence.AMBIGUOUS
    assert not result.dogs[0].foot_in


def test_a_weak_detection_on_a_cushion_cannot_be_on(mask):
    result = classify(mask, [dog((0.45, 0.25, 0.55, 0.58), confidence=0.25)])
    assert result.evidence is Evidence.AMBIGUOUS
    assert result.dogs[0].support >= CFG.on_support  # geometry agrees; confidence vetoes


def test_a_weak_floor_detection_does_not_account_for_the_dog(mask):
    result = classify(mask, [dog((0.30, 0.75, 0.42, 0.95), confidence=0.25)])
    assert result.dogs[0].label == LABEL_WEAK
    assert result.evidence is Evidence.CLEAR_EMPTY


def test_people_never_suppress_a_dog_on_the_couch(mask):
    result = classify(mask, [dog((0.60, 0.30, 0.72, 0.60))], [person((0.25, 0.10, 0.50, 0.70))])
    assert result.evidence is Evidence.ON


def test_a_person_overlapping_the_dog_still_does_not_suppress_it(mask):
    result = classify(mask, [dog((0.45, 0.30, 0.60, 0.58))], [person((0.40, 0.10, 0.55, 0.60))])
    assert result.evidence is Evidence.ON


def test_dog_hidden_below_by_a_person_is_not_called_floor(mask):
    """Only a head above someone's shoulder: its box ends above the cushions."""
    head = (0.45, 0.22, 0.55, 0.38)
    result = classify(mask, [dog(head)], [person((0.40, 0.15, 0.62, 0.70))])
    assert result.dogs[0].foot_unreliable == "person_occlusion"
    assert result.dogs[0].label == LABEL_AMBIGUOUS
    assert result.evidence is Evidence.AMBIGUOUS


def test_box_cut_by_the_frame_is_flagged(mask):
    scored = score_dog(dog((0.30, 0.80, 0.42, 1.0)), mask, [], CFG, DOG_CONF)
    assert scored.foot_unreliable == "frame_edge"


def test_no_dog_with_cushions_mostly_hidden_is_obstructed_not_clear(mask):
    result = classify(mask, [], [person((0.15, 0.10, 0.75, 0.90))])
    assert result.visible_fraction < 0.5
    assert result.evidence is Evidence.OBSTRUCTED


def test_no_dog_with_a_small_person_overlap_is_clear_with_reduced_visibility(mask):
    result = classify(mask, [], [person((0.78, 0.30, 0.92, 0.90))])
    assert result.evidence is Evidence.CLEAR_EMPTY
    assert 0.5 < result.visible_fraction < 0.95


def test_any_dog_on_a_cushion_decides_the_frame(mask):
    result = classify(mask, [dog((0.30, 0.75, 0.42, 0.95)), dog((0.45, 0.25, 0.55, 0.58))])
    assert result.evidence is Evidence.ON
    assert sorted(d.label for d in result.dogs) == [LABEL_OFF, LABEL_ON]


def test_ambiguous_outranks_floor_when_aggregating(mask):
    result = classify(mask, [dog((0.30, 0.75, 0.42, 0.95)), dog((0.40, 0.45, 0.52, 0.80))])
    assert result.evidence is Evidence.AMBIGUOUS


def test_support_is_independent_of_model_confidence(mask):
    box = (0.45, 0.25, 0.55, 0.58)
    high = score_dog(dog(box, 0.99), mask, [], CFG, DOG_CONF)
    low = score_dog(dog(box, 0.21), mask, [], CFG, DOG_CONF)
    assert high.support == low.support


def test_disabled_polygons_do_not_count():
    empty = CushionMask([], 1280, 720)
    result = classify_frame([dog((0.45, 0.25, 0.55, 0.58))], [], empty, CFG, DOG_CONF)
    assert result.evidence is Evidence.CLEAR_SEEN
