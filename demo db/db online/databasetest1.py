from pymongo import MongoClient
uri = "mongodb+srv://valentinostraw_db_user:js0HcmPQYrbAwQcN@whytehyena.7eyqwi6.mongodb.net/"
client = MongoClient(uri)

print(client.list_database_names())

dblist = client.list_database_names()
if "mydatabase" in dblist:
  print("The database exists.")

mycol = client["customers"]

collist = mydb.list_collection_names()
if "customers" in collist:
  print("The collection exists.")

try:
    client.admin.command("ping")
    print("Connected successfully")
    client.close()

except Exception as e:
    raise Exception(
        "The following error occurred: ", e)