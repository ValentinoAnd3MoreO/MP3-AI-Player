import unittest
from unittest.mock import Mock, patch

from scripts.enrich_metadata import (
    AlbumEnrichment,
    _album_groups,
    _genre_tags,
    _needs_year,
    _normalize_album,
    _updates_for_group,
    enrich_collection,
    lookup_release_group,
    match_release_group,
)


class MetadataNormalizationTests(unittest.TestCase):
    def test_normalizes_accents_and_edition_suffix(self):
        self.assertEqual(_normalize_album("Ahí Vamos (Remastered)"), "ahi vamos")

    def test_year_1970_is_missing_but_valid_year_is_not(self):
        self.assertTrue(_needs_year({"year": 1970}))
        self.assertTrue(_needs_year({"year": "1970"}))
        self.assertFalse(_needs_year({"year": 1977}))


class MusicBrainzMatchingTests(unittest.TestCase):
    def test_retries_temporary_musicbrainz_unavailability(self):
        session = Mock()
        unavailable = Mock(status_code=503, headers={"Retry-After": "2"})
        success = Mock(
            status_code=200,
            json=Mock(return_value={"release-groups": []}),
        )
        session.get.side_effect = [unavailable, success]
        with patch("scripts.enrich_metadata.time.sleep") as sleep:
            result = lookup_release_group(session, "Artist", "Album")
        self.assertEqual(result.status, "no_match")
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_accepts_exact_artist_album_and_known_genre_tags(self):
        result = match_release_group(
            "Deftones",
            "White Pony",
            {
                "release-groups": [
                    {
                        "id": "release-id",
                        "score": 100,
                        "title": "White Pony",
                        "primary-type": "Album",
                        "artist-credit": [{"name": "Deftones"}],
                        "first-release-date": "2000-06-20",
                        "tags": [
                            {"name": "alternative metal", "count": 10},
                            {"name": "horse on cover", "count": 100},
                            {"name": "nu metal", "count": 1},
                        ],
                    }
                ]
            },
        )
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.year, 2000)
        self.assertEqual(result.genres, ("alternative metal",))
        self.assertEqual(result.release_group_id, "release-id")

    def test_rejects_similar_but_not_exact_artist_or_album(self):
        result = match_release_group(
            "Deftones",
            "White Pony",
            {
                "release-groups": [
                    {
                        "score": 100,
                        "title": "White Pony (Live)",
                        "primary-type": "Album",
                        "artist-credit": [{"name": "Deftones Tribute"}],
                        "first-release-date": "2000",
                    }
                ]
            },
        )
        self.assertEqual(result.status, "no_match")

    def test_uses_earliest_year_for_exact_album_matches(self):
        release_groups = [
            {
                "score": 100,
                "title": "Album",
                "primary-type": "Album",
                "artist-credit": [{"name": "Artist"}],
                "first-release-date": year,
            }
            for year in ("1990", "1991")
        ]
        result = match_release_group(
            "Artist", "Album", {"release-groups": release_groups}
        )
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.year, 1990)

    def test_album_edition_can_match_original_release_group(self):
        result = match_release_group(
            "Fleetwood Mac",
            "Rumours (Super Deluxe)",
            {
                "release-groups": [
                    {
                        "score": 100,
                        "title": "Rumours",
                        "primary-type": "Album",
                        "artist-credit": [{"name": "Fleetwood Mac"}],
                        "first-release-date": "1977-02-04",
                    }
                ]
            },
        )
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.year, 1977)

    def test_selects_first_album_release_and_ignores_ep_with_same_title(self):
        response_data = {
            "release-groups": [
                {
                    "score": 100,
                    "title": "Deftones",
                    "primary-type": "EP",
                    "artist-credit": [{"name": "Deftones"}],
                    "first-release-date": "1992",
                },
                {
                    "score": 93,
                    "title": "Deftones",
                    "primary-type": "Album",
                    "artist-credit": [{"name": "Deftones"}],
                    "first-release-date": "2003-05-19",
                },
            ]
        }
        result = match_release_group("Deftones", "Deftones", response_data)
        self.assertEqual(result.status, "matched")
        self.assertEqual(result.year, 2003)


class MetadataUpdateTests(unittest.TestCase):
    def test_updates_only_missing_genre_and_unreliable_year(self):
        group = {
            "documents": [
                {"_id": "one", "year": 1970},
                {"_id": "two", "year": 1980, "genre": ["rock"]},
            ]
        }
        updates = _updates_for_group(
            group,
            AlbumEnrichment(
                "matched",
                year=1977,
                genres=("rock",),
                release_group_id="release-id",
            ),
        )
        self.assertEqual(len(updates), 1)
        self.assertEqual(
            updates[0][1],
            {
                "year": 1977,
                "decade": 1970,
                "year_source": "MusicBrainz release-group first-release-date",
                "genre": ["rock"],
                "genre_source": "MusicBrainz community release-group tags",
                "metadata_confidence": "high",
                "musicbrainz_release_group_id": "release-id",
            },
        )

    def test_groups_album_track_artist_credits_by_primary_artist(self):
        groups = _album_groups(
            [
                {"_id": 1, "artist": "Artist", "album": "Album"},
                {"_id": 2, "artist": "Artist/Guest", "album": "Album"},
            ]
        )
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["artist"], "Artist")
        self.assertEqual(len(groups[0]["documents"]), 2)

    def test_preview_does_not_write_to_mongodb(self):
        collection = Mock()
        collection.find.return_value = [
            {
                "_id": "one",
                "artist": "Artist",
                "album": "Album",
                "year": 1970,
            }
        ]
        with patch(
            "scripts.enrich_metadata.lookup_release_group",
            return_value=AlbumEnrichment(
                "matched", year=1990, genres=("rock",), release_group_id="id"
            ),
        ):
            result = enrich_collection(collection, apply=False)
        collection.update_one.assert_not_called()
        self.assertEqual(result[0]["would_update"], 1)
        self.assertEqual(result[0]["would_set_year"], 1)
        self.assertEqual(result[0]["would_set_genre"], 1)

    def test_apply_updates_only_records_with_changes(self):
        collection = Mock()
        collection.find.return_value = [
            {
                "_id": "one",
                "artist": "Artist",
                "album": "Album",
                "year": 1970,
            }
        ]
        collection.update_one.return_value = Mock(modified_count=1)
        with patch(
            "scripts.enrich_metadata.lookup_release_group",
            return_value=AlbumEnrichment(
                "matched", year=1990, genres=("rock",), release_group_id="id"
            ),
        ):
            result = enrich_collection(collection, apply=True)
        collection.update_one.assert_called_once()
        self.assertEqual(result[0]["updated"], 1)


if __name__ == "__main__":
    unittest.main()
