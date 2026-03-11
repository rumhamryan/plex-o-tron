from telegram_bot.services.media_manager import (
    generate_plex_filename,
    parse_resolution_from_name,
    get_dominant_file_type,
)


class DummyFiles:
    def num_files(self):
        return 1

    def file_path(self, index):
        return "Movie.mkv"

    def file_size(self, index):
        return 1024


def test_media_manager_contracts():
    parsed = {"type": "movie", "title": "Inception", "year": "2010"}
    assert generate_plex_filename(parsed, ".mkv") == "Inception (2010).mkv"
    assert parse_resolution_from_name("Movie.2160p.BluRay") == "4K"
    assert get_dominant_file_type(DummyFiles()) == "MKV"
