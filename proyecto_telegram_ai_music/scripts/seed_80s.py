import re
import unicodedata

from pymongo import MongoClient, UpdateOne

from app.config import Settings

SONGS = [
    ("Another One Bites the Dust", "Queen", 1980, "rock"),
    ("Call Me", "Blondie", 1980, "new wave"),
    ("You Shook Me All Night Long", "AC/DC", 1980, "hard rock"),
    ("Celebration", "Kool & The Gang", 1980, "funk"),
    ("Don't Stop Believin'", "Journey", 1981, "rock"),
    ("Tainted Love", "Soft Cell", 1981, "synth-pop"),
    ("Under Pressure", "Queen & David Bowie", 1981, "rock"),
    ("Every Little Thing She Does Is Magic", "The Police", 1981, "rock"),
    ("In the Air Tonight", "Phil Collins", 1981, "pop rock"),
    ("Start Me Up", "The Rolling Stones", 1981, "rock"),
    ("Jessie's Girl", "Rick Springfield", 1981, "pop rock"),
    ("Physical", "Olivia Newton-John", 1981, "pop"),
    ("Don't You Want Me", "The Human League", 1981, "synth-pop"),
    ("I Love Rock 'n Roll", "Joan Jett & the Blackhearts", 1981, "rock"),
    ("Billie Jean", "Michael Jackson", 1982, "pop"),
    ("Beat It", "Michael Jackson", 1982, "pop rock"),
    ("Africa", "Toto", 1982, "pop rock"),
    ("Eye of the Tiger", "Survivor", 1982, "rock"),
    ("1999", "Prince", 1982, "funk"),
    ("Maneater", "Daryl Hall & John Oates", 1982, "pop rock"),
    ("Rosanna", "Toto", 1982, "pop rock"),
    ("Hungry Like the Wolf", "Duran Duran", 1982, "new wave"),
    ("Come On Eileen", "Dexys Midnight Runners", 1982, "pop"),
    ("Do You Really Want to Hurt Me", "Culture Club", 1982, "new wave"),
    ("Down Under", "Men at Work", 1981, "new wave"),
    ("Rock the Casbah", "The Clash", 1982, "rock"),
    ("Sexual Healing", "Marvin Gaye", 1982, "R&B"),
    ("Sweet Dreams (Are Made of This)", "Eurythmics", 1983, "synth-pop"),
    ("Every Breath You Take", "The Police", 1983, "rock"),
    ("Girls Just Want to Have Fun", "Cyndi Lauper", 1983, "pop"),
    ("Time After Time", "Cyndi Lauper", 1983, "pop"),
    ("Total Eclipse of the Heart", "Bonnie Tyler", 1983, "pop rock"),
    ("Let's Dance", "David Bowie", 1983, "dance rock"),
    ("Karma Chameleon", "Culture Club", 1983, "pop"),
    ("Uptown Girl", "Billy Joel", 1983, "pop rock"),
    ("The Safety Dance", "Men Without Hats", 1982, "new wave"),
    ("True", "Spandau Ballet", 1983, "new wave"),
    ("Owner of a Lonely Heart", "Yes", 1983, "rock"),
    ("Islands in the Stream", "Kenny Rogers & Dolly Parton", 1983, "country pop"),
    ("All Night Long (All Night)", "Lionel Richie", 1983, "pop"),
    ("When Doves Cry", "Prince", 1984, "pop"),
    ("Purple Rain", "Prince", 1984, "rock"),
    ("Let's Go Crazy", "Prince", 1984, "rock"),
    ("Like a Virgin", "Madonna", 1984, "pop"),
    ("Material Girl", "Madonna", 1984, "pop"),
    ("Wake Me Up Before You Go-Go", "Wham!", 1984, "pop"),
    ("Careless Whisper", "George Michael", 1984, "pop"),
    ("I Want to Know What Love Is", "Foreigner", 1984, "rock"),
    ("Summer of '69", "Bryan Adams", 1984, "rock"),
    ("Heaven", "Bryan Adams", 1984, "rock"),
    ("What's Love Got to Do with It", "Tina Turner", 1984, "pop"),
    ("Jump", "Van Halen", 1984, "rock"),
    ("99 Luftballons", "Nena", 1983, "new wave"),
    ("The Reflex", "Duran Duran", 1984, "new wave"),
    ("Relax", "Frankie Goes to Hollywood", 1983, "synth-pop"),
    ("Shout", "Tears for Fears", 1984, "new wave"),
    ("Everybody Wants to Rule the World", "Tears for Fears", 1985, "new wave"),
    ("Take on Me", "a-ha", 1985, "synth-pop"),
    ("Money for Nothing", "Dire Straits", 1985, "rock"),
    ("Sussudio", "Phil Collins", 1985, "pop"),
    ("Don't You (Forget About Me)", "Simple Minds", 1985, "rock"),
    ("We Built This City", "Starship", 1985, "rock"),
    ("Running Up That Hill", "Kate Bush", 1985, "pop"),
    ("We Don't Need Another Hero", "Tina Turner", 1985, "pop rock"),
    ("Part-Time Lover", "Stevie Wonder", 1985, "R&B"),
    ("Into the Groove", "Madonna", 1985, "dance-pop"),
    ("The Power of Love", "Huey Lewis and the News", 1985, "rock"),
    ("Walk of Life", "Dire Straits", 1985, "rock"),
    ("Raspberry Beret", "Prince", 1985, "pop"),
    ("Kiss", "Prince", 1986, "funk"),
    ("Livin' on a Prayer", "Bon Jovi", 1986, "rock"),
    ("You Give Love a Bad Name", "Bon Jovi", 1986, "rock"),
    ("Papa Don't Preach", "Madonna", 1986, "pop"),
    ("True Colors", "Cyndi Lauper", 1986, "pop"),
    ("Walk Like an Egyptian", "The Bangles", 1986, "pop rock"),
    ("Manic Monday", "The Bangles", 1986, "pop rock"),
    ("The Final Countdown", "Europe", 1986, "rock"),
    ("Danger Zone", "Kenny Loggins", 1986, "rock"),
    ("Sledgehammer", "Peter Gabriel", 1986, "pop rock"),
    ("West End Girls", "Pet Shop Boys", 1985, "synth-pop"),
    ("Bizarre Love Triangle", "New Order", 1986, "new wave"),
    ("I Wanna Dance with Somebody (Who Loves Me)", "Whitney Houston", 1987, "pop"),
    ("With or Without You", "U2", 1987, "rock"),
    ("Sweet Child o' Mine", "Guns N' Roses", 1987, "hard rock"),
    ("Never Gonna Give You Up", "Rick Astley", 1987, "synth-pop"),
    ("Faith", "George Michael", 1987, "pop"),
    ("Bad", "Michael Jackson", 1987, "pop"),
    ("The Way You Make Me Feel", "Michael Jackson", 1987, "pop"),
    ("Heaven Is a Place on Earth", "Belinda Carlisle", 1987, "pop rock"),
    ("Need You Tonight", "INXS", 1987, "rock"),
    ("Here I Go Again", "Whitesnake", 1987, "hard rock"),
    ("Fast Car", "Tracy Chapman", 1988, "folk rock"),
    ("Every Rose Has Its Thorn", "Poison", 1988, "rock"),
    ("The Look", "Roxette", 1988, "pop rock"),
    ("Eternal Flame", "The Bangles", 1989, "pop"),
    ("Like a Prayer", "Madonna", 1989, "pop"),
    ("Personal Jesus", "Depeche Mode", 1989, "synth-pop"),
    ("Another Day in Paradise", "Phil Collins", 1989, "pop"),
    ("Love Shack", "The B-52's", 1989, "new wave"),
    ("Pour Some Sugar on Me", "Def Leppard", 1987, "hard rock"),
]


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")


def build_documents() -> list[dict]:
    if len(SONGS) != 100:
        raise ValueError(f"El catálogo debe tener 100 canciones, tiene {len(SONGS)}.")

    documents = []
    for title, artist, year, genre in SONGS:
        mood = ["movido", "bailable"] if genre in {
            "dance rock", "dance-pop", "disco", "funk", "synth-pop"
        } else ["enérgico"] if "rock" in genre or "metal" in genre else ["popular"]
        documents.append(
            {
                "_id": f"seed80-{_slug(artist)}-{_slug(title)}",
                "title": title,
                "artist": artist,
                "year": year,
                "decade": 1980,
                "genre": [genre],
                "mood": mood,
                "tags": ["80s", "hit", genre],
                "source": "curated_seed_80s",
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
        operations = [
            UpdateOne({"_id": document["_id"]}, {"$setOnInsert": document}, upsert=True)
            for document in documents
        ]
        result = collection.bulk_write(operations, ordered=False)
        total = collection.count_documents({"source": "curated_seed_80s"})
        print(
            f"Catálogo procesado: {len(documents)}; "
            f"nuevas canciones: {result.upserted_count}; "
            f"ya existentes: {result.matched_count}; "
            f"total del catálogo: {total}; "
            f"destino: {settings.mongo_db}.{settings.mongo_collection}"
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
