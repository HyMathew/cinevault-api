# Représentation des instantanés pour la migration MongoDB

Le module `app.persistence.migration_codec` convertit un document JSON v2
validé vers douze listes de documents BSON, puis réalise la conversion inverse.
Il réutilise les codecs de persistance pour les identifiants, les entiers Int64
et les champs techniques de recherche. L'inverse vérifie l'ensemble exact des
collections, la révision, les références métier et la correspondance bijective
entre les entités persistées et leur registre. Les réponses historiques et les
échéances des reçus restent identiques, y compris lorsque l'état métier courant
a changé depuis leur création.

Le rang des événements d'audit provient du tableau JSON lors de l'aller et du
champ `_ordinal` lors du retour. Une série d'ordinaux distincts peut commencer
après 1 ou contenir des intervalles. La conversion conserve l'ordre relatif,
seul ordre d'audit représentable en JSON v2. Un nouvel aller numérote de 1 à N.
Les autres listes racines utilisent un tri par identifiant ou clé de tentative
de connexion. Les séquences FIFO et l'ordre des tableaux internes restent
inchangés. L'export catalogue historique suit l'insertion JSON. Sa parité entre
moteurs exige un ordre contractuel commun au niveau de ce service. Le codec
conserve également les films d'identifiants distincts qui partagent une clé
naturelle. Le service d'import doit traiter cette ambiguïté explicitement
avant basculement. Sa sélection actuelle du dernier film dépend de l'ordre
des lignes.

Chaque ligne BSON peut être fournie comme dictionnaire natif ou comme
`RawBSONDocument`. La lecture brute permet de détecter les clés répétées et les
index de tableau non canoniques que le décodage ordinaire ferait disparaître.
Les validations inverses refusent les int32
même si leur valeur pourrait être représentée en JSON. Le point d'entrée
fichier doit employer le parseur strict `storage_v2.decode_document` pour
refuser les clés JSON répétées avant conversion.

La validation JSON v2 actuelle reste la limite du volume réversible.
Un état Mongo au-delà d'un plafond donne une erreur sans troncature. Le
module traite des instantanés matérialisés et conserve des copies en mémoire
pendant la conversion. La cohérence temporelle de l'instantané et les limites
opérationnelles relèvent du futur outil de migration, qui devra également
vérifier les options et index natifs, figer les écritures et conserver une
sauvegarde avant publication. Les essais de ce module couvrent les conversions
de documents. Le scénario P5 du basculement, des nouvelles écritures HTTP
Mongo, du retour arrière et du redémarrage nécessite les outils d'exploitation.
