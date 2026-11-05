import re
import unicodedata

from pymongo import MongoClient, UpdateOne

from app.config import Settings

ALTERNATIVE_ROCK = [
    ("Radio Free Europe", "R.E.M.", 1981),
    ("Driver 8", "R.E.M.", 1985),
    ("The One I Love", "R.E.M.", 1987),
    ("Orange Crush", "R.E.M.", 1988),
    ("Losing My Religion", "R.E.M.", 1991),
    ("Man on the Moon", "R.E.M.", 1992),
    ("What's the Frequency, Kenneth?", "R.E.M.", 1994),
    ("Just Like Heaven", "The Cure", 1987),
    ("Lovesong", "The Cure", 1989),
    ("Pictures of You", "The Cure", 1989),
    ("There Is a Light That Never Goes Out", "The Smiths", 1986),
    ("How Soon Is Now?", "The Smiths", 1984),
    ("This Charming Man", "The Smiths", 1983),
    ("Where Is My Mind?", "Pixies", 1988),
    ("Here Comes Your Man", "Pixies", 1989),
    ("Debaser", "Pixies", 1989),
    ("Cannonball", "The Breeders", 1993),
    ("Smells Like Teen Spirit", "Nirvana", 1991),
    ("Come as You Are", "Nirvana", 1991),
    ("Lithium", "Nirvana", 1991),
    ("In Bloom", "Nirvana", 1991),
    ("Heart-Shaped Box", "Nirvana", 1993),
    ("Alive", "Pearl Jam", 1991),
    ("Even Flow", "Pearl Jam", 1991),
    ("Black", "Pearl Jam", 1991),
    ("Daughter", "Pearl Jam", 1993),
    ("Black Hole Sun", "Soundgarden", 1994),
    ("Spoonman", "Soundgarden", 1994),
    ("Man in the Box", "Alice in Chains", 1990),
    ("Would?", "Alice in Chains", 1992),
    ("No Excuses", "Alice in Chains", 1994),
    ("Plush", "Stone Temple Pilots", 1992),
    ("Interstate Love Song", "Stone Temple Pilots", 1994),
    ("Vasoline", "Stone Temple Pilots", 1994),
    ("Under the Bridge", "Red Hot Chili Peppers", 1991),
    ("Give It Away", "Red Hot Chili Peppers", 1991),
    ("Soul to Squeeze", "Red Hot Chili Peppers", 1993),
    ("Creep", "Radiohead", 1992),
    ("Fake Plastic Trees", "Radiohead", 1995),
    ("Karma Police", "Radiohead", 1997),
    ("Live Forever", "Oasis", 1994),
    ("Wonderwall", "Oasis", 1995),
    ("Don't Look Back in Anger", "Oasis", 1995),
    ("Song 2", "Blur", 1997),
    ("Girls & Boys", "Blur", 1994),
    ("1979", "The Smashing Pumpkins", 1995),
    ("Today", "The Smashing Pumpkins", 1993),
    ("Cherub Rock", "The Smashing Pumpkins", 1993),
    ("Lullaby", "The Cure", 1989),
    ("Epic", "Faith No More", 1989),
    ("Midlife Crisis", "Faith No More", 1992),
    ("Flagpole Sitta", "Harvey Danger", 1997),
    ("Closing Time", "Semisonic", 1998),
]

PURPLE_RAIN = ("Purple Rain", "Prince", 1984)


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


def build_documents() -> list[dict]:
    tracks = [(title, artist, year, True) for title, artist, year in ALTERNATIVE_ROCK]
    tracks.append((*PURPLE_RAIN, False))
    documents = []
    for title, artist, year, is_alternative in tracks:
        genre = ["alternative rock"] if is_alternative else ["rock"]
        tags = ["80s" if year < 1990 else "90s", "alternative rock"]
        if not is_alternative:
            tags = ["80s", "classic", "rock anthem", "soundtrack"]
        documents.append(
            {
                "_id": f"seed80-{_slug(artist)}-{_slug(title)}",
                "title": title,
                "artist": artist,
                "year": year,
                "decade": (year // 10) * 10,
                "genre": genre,
                "mood": ["enérgico"] if is_alternative else ["épico", "emotivo"],
                "tags": tags,
                "source": (
                    "curated_alternative_rock"
                    if is_alternative
                    else "curated_featured_track"
                ),
            }
        )
    return documents


def main() -> None:
    settings = Settings.from_env()
    client = MongoClient(
        settings.mongo_uri,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
    )
    try:
        client.admin.command("ping")
        collection = client[settings.mongo_db][settings.mongo_collection]
        documents = build_documents()
        operations = []
        for document in documents:
            if document["source"] == "curated_featured_track":
                identity = {
                    "title": {"$regex": f"^{re.escape(document['title'])}$", "$options": "i"},
                    "artist": {"$regex": f"^{re.escape(document['artist'])}$", "$options": "i"},
                }
            else:
                identity = {"_id": document["_id"]}
            operations.append(
                UpdateOne(identity, {"$setOnInsert": document}, upsert=True)
            )
        result = collection.bulk_write(operations, ordered=False)
        alternative_count = collection.count_documents(
            {"source": "curated_alternative_rock"}
        )
        purple_rain_count = collection.count_documents(
            {"title": "Purple Rain", "artist": "Prince"}
        )
        print(
            f"Catálogo procesado: {len(documents)}; "
            f"nuevas canciones: {result.upserted_count}; "
            f"ya existentes: {result.matched_count}; "
            f"rock alternativo en catálogo: {alternative_count}; "
            f"Purple Rain encontrado: {purple_rain_count > 0}; "
            f"destino: {settings.mongo_db}.{settings.mongo_collection}"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
