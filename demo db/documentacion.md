# MongoDB

## crear una base de datos 

Con el siguiente codigo creamos una base de datos y verificamos que exista, gracias a la libreria pymongo  

```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")

dblist = myclient.list_database_names() 
if "mydatabase" in dblist:
  print("The database exists.")
```

## crear una coleccion 
con el siguiente codigo creo y me fijo si existe una coleccion
```py
import pymongo
myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]
collist = mydb.list_collection_names()
if "customers" in collist:
  print("The collection exists.")
```
## Crear un item 
con el siguiente codigo añadimos un item a la coleccion insert.one() es la clave para añadir el item  , pero al no especificar el id el programa va crear un item aleatorio 
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

mydict = { "name": "John", "address": "Highway 37" }

x = mycol.insert_one(mydict) 
```
## como fijarse el id de mi item 
```py
mydict = { "name": "Peter", "address": "Lowstreet 27" }

x = mycol.insert_one(mydict)

print(x.inserted_id) 
```
## como crear varios items y con ids especificos 
para poder hacer esto va ser clave, el cuando pongamos nuestro items en el diccionario vamos a poner los ids primero,Y MUY IMPORTANTE NO SE PUEDE REPETIR IDS,y para insertar más de un item insert.many()
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

mylist = [
  { "_id": 1, "name": "John", "address": "Highway 37"},
  { "_id": 2, "name": "Peter", "address": "Lowstreet 27"},
  { "_id": 3, "name": "Amy", "address": "Apple st 652"},
  { "_id": 4, "name": "Hannah", "address": "Mountain 21"},
  { "_id": 5, "name": "Michael", "address": "Valley 345"},
  { "_id": 6, "name": "Sandy", "address": "Ocean blvd 2"},
  { "_id": 7, "name": "Betty", "address": "Green Grass 1"},
  { "_id": 8, "name": "Richard", "address": "Sky st 331"},
  { "_id": 9, "name": "Susan", "address": "One way 98"},
  { "_id": 10, "name": "Vicky", "address": "Yellow Garden 2"},
  { "_id": 11, "name": "Ben", "address": "Park Lane 38"},
  { "_id": 12, "name": "William", "address": "Central st 954"},
  { "_id": 13, "name": "Chuck", "address": "Main Road 989"},
  { "_id": 14, "name": "Viola", "address": "Sideway 1633"}
]

x = mycol.insert_many(mylist)

#print list of the _id values of the inserted documents:
print(x.inserted_ids) 
```
## Buscar un item 
para buscar un item usaremos find_one() que en caso de no poner nada en el parentesis buscara solo el primer archivo de nuestra base de datos
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

x = mycol.find_one()

print(x) 
```
## Buscar varios items 
para buscar todos los items usaremos find()
```py
import pymongo
myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

for x in mycol.find():
  print(x) 
```
## Busquedas especiales 
si nosotros queremos ver la lista de datos, pero omitiendo una parte, por ej el id pondremos en 0, y en 1 las columnas que si queremos ver 
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

for x in mycol.find({},{ "_id": 0, "name": 1, "address": 1 }):
  print(x) 
```
para ahora buscar una cosa con una condicion especifica crearemos una variable que sea lo que queremos buscar y despues con find lo buscamos
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

myquery = { "address": "Park Lane 38" }

mydoc = mycol.find(myquery)

for x in mydoc:
  print(x) 
```
ahora buscar a valores con algo mayor en numero o letras usaremos "$gt", por ej "$gt": "S" buscara valores mayores a S
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

myquery = { "address": { "$gt": "S" } }

mydoc = mycol.find(myquery)

for x in mydoc:
  print(x) 
```
ahora buscar a valores con algo menor en numero o letras usaremos "$lt", por ej "$lt": "S" buscara valores menores a S
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

myquery = { "address": { "$lt": "S" } }

mydoc = mycol.find(myquery)

for x in mydoc:
  print(x) 
```
ahora para buscar archivos que empiecen con una letra usaremos el comando $regex
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

myquery = { "address": { "$regex": "^S" } }

mydoc = mycol.find(myquery)

for x in mydoc:
  print(x) 
```
## Eliminar documentos 
para eliminar un dato usaremos delete_one() y dentro del parentesis podremos el parametro de busqueda que queremos eliminar que es una variable que definimos antes 
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

myquery = { "address": "Mountain 21" }

mycol.delete_one(myquery) 
```
## Eliminar mas de un item 
para poder eliminar mas de un item usaremos el delete_many() y dentro del parentesis una variable con las condiciones de que quiero eliminar 
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

myquery = { "address": {"$regex": "^S"} }

x = mycol.delete_many(myquery)

print(x.deleted_count, " documents deleted.") 
```
para eliminar todos usaremos el delete many pero no pondremos nada dentro del parentesis dentro de el delete_many 
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

x = mycol.delete_many({})

print(x.deleted_count, " documents deleted.") 
```
## Actualizar un item 
para poder actualizar un dato dentro de la base de datos tendremos que usar el update_one(), dentro del el habra 2 variables, la primera es la que vamos a cambiar, y la segunda con la que vamos a cambiar 
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

myquery = { "address": "Valley 345" }
newvalues = { "$set": { "address": "Canyon 123" } }# con esto seleccionamos los campos que queremos cambiar en un diccionario 

mycol.update_one(myquery, newvalues)

#print "customers" after the update:
for x in mycol.find():
  print(x) 
```
## actualizar más de un item 
para poder actulizar más de uno usaremos el update_many(), que se tiene que hacer lo mismo que el update_one()
```py
import pymongo

myclient = pymongo.MongoClient("mongodb://localhost:27017/")
mydb = myclient["mydatabase"]
mycol = mydb["customers"]

myquery = { "address": { "$regex": "^S" } }
newvalues = { "$set": { "name": "Minnie" } }

x = mycol.update_many(myquery, newvalues)

print(x.modified_count, "documents updated.") 
```




