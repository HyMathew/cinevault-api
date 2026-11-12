# Collections privées et journal — lot C3

`PersonalCollectionService` porte les cinq routes collections et la lecture admin
du journal. Les contrôleurs transmettent des arguments Python et rendent les
`ServiceResult` après commit ; ni Flask ni PyMongo n’entrent dans le service.
La composition HTTP sélectionne toujours JSON, sur le même fichier et verrou que
les domaines legacy. Ce lot n’active pas le backend Mongo global.

## Autorité, propriété et pagination

`authenticated` relit le compte et la session dans la même UoW que l’opération.
Un administrateur ou gestionnaire n’a aucune exception à la confidentialité des
collections : compte étranger et ID absent donnent404. Mongo applique owner_id
dans get, page/count, replace et delete. Les mises à jour n’acceptent ni changement
de propriétaire, ni film ou type différent. Unicode casefold pour le nom de liste,
unicité par propriétaire/film/type/nom, ETag et préconditions428/412 sont conservés.

La page est filtrée par propriétaire avant tri created_at/UUID descendant et
pagination. Seuls les IDs de films présents sur cette page (au plus100) sont lus,
en une sélection ciblée. Le filtre list_name ne fait pas de strip : sa borne80
porte sur le texte brut, puis sa clé casefold peut atteindre240caractères.

Le contrôle HTTP précède le parsing comme auparavant : authentification pour POST,
propriété et If-Match pour PUT, rôle admin pour les paramètres audit. L’autorité
est revérifiée par la commande finale. GET audit conserve le premier limit/offset
lorsqu’il est répété ; collections GET continue de refuser les doublons. Les
lectures protégées touchent leur session comme dans le contrat initial.

Le [précontrôle avant parsing](authorization-preflight.md) utilise en revanche
la même autorité dans une query sans touch ni révision. Ce choix corrige les
commits préparatoires qui pouvaient persister malgré une erreur400/422 ; la
commande finale relit toujours l’autorité et réalise seule touch+mutation.

## Audit atomique et ordre exact

Une mutation réussie écrit l’entrée et son événement dans la même transaction.
Les UUID d’entrée et d’événement et l’horodatage de l’opération sont préparés hors
callback réexécutable. Le journal contient uniquement id, actor_id, entity_type,
entity_id, action et timestamp ; aucun nom de liste, support, formulaire ou secret.
entity_id peut désigner une entrée supprimée. L’acteur système null reste limité
à l’expiration de réservation conformément au validateur existant.

JSON conserve l’ordre d’append et exactement le suffixe des2000événements les plus
récents. Mongo utilise un Int64 technique `_ordinal`, positif, réservé et caché au
décodage. Sous le coordinateur global déjà acquis, append lit le dernier ordinal
avec l’index unique audit_append_order, choisit max+1 et insère. Deux événements
d’une même seconde, une horloge reculée et plusieurs append dans une UoW gardent
ainsi leur ordre réel, indépendamment du timestamp ou d’un UUID aléatoire.

La rétention supprime seulement les événements précédant le suffixe2000 ; leurs
UUID sont libérés du registre dans la même transaction, par lots de500 au plus.
Un échec annule aussi l’entrée créée, les événements, la révision et le registre.
L’épuisement de l’ordinal Int64 provoque un refus, jamais une remise à zéro.
Aucun prêt, réservation ou autre historique métier n’est purgé par cette politique.

L’ancien `record_event(document, ...)` reste employé par les domaines non encore
extraits. Il partage le constructeur minimal et la constante2000 ; son tableau
JSON public reste identique. Cette adaptation ne signifie pas que ces autres
services possèdent déjà tous des identités préparées hors callback.

## Évolution native et migration obligatoire

`encode_record('audit_events', event, audit_ordinal=n)` exige explicitement un rang
entier positif ; il n’invente pas de rang1 par défaut. Le décodeur exige un vrai
BSON Int64 valide puis retire `_ordinal`. La validation et l’index unique refusent
champ absent, mauvais type, rang hors limites ou doublon. L’ancien index d’ordre
timestamp/UUID est remplacé dans les spécifications par audit_append_order ; le
total reste25index déclarés, plus les index `_id` automatiques.

Une ancienne base Mongo sans ordinal est **incompatible** avec ce validateur.
Le provisionnement refuse cette divergence sans collMod, backfill silencieux ou
désactivation de sécurité. La migration devra attribuer1..N dans l’ordre du
tableau JSON retenu, sans trier les timestamps. Elle ne reconstitue pas les anciens
événements déjà sortis du journal. L’export inverse trie ordinal croissant puis
retire ce champ ; dump/restore Mongo doit conserver les ordinaux exacts. Ce lot
documente cette exigence, il ne livre pas encore les outils de migration.

## Capacité encore ouverte

L’extraction ne résout pas le constat CWE-770 des places de collections partagées.
JSON conserve son plafond physique global10000 et le refus503 sans mutation
partielle. Aucun plafond physique général10000 n’est ajouté à Mongo. L’admission
équitable et les budgets de création par membre restent un chantier dédié ;
l’absence de cette ancienne limite BSON ne constitue pas une défense anti-abus.
Le journal circulaire2000 est une rétention existante explicitement bornée, et
ne doit pas être confondu avec une purge des historiques métier pour gagner de la
place. Les journaux de sécurité d’authentification et l’archivage externe restent
également des exigences séparées.

## Vérifications

`tests/test_personal_service.py` exécute le même contrat sur un vrai fichier JSON
ou via `exercise_personal_service(database, scope_observer=None)` sur un serveur
jetable provisionné. Il couvre propriété même face à un admin, filtre avant page,
Unicode3x, ETag, refus, audit minimal, conflit concurrent et annulation atomique.
Les fixtures natives de rétention installent2501événements synthétiques, puis un
vrai append doit garder le suffixe2000 et libérer le registre en plusieurs lots.
Les contraintes natives, parents manquants et épuisement ordinal sont contrôlés.

Le gate CI Mongo attache un CommandListener qui ne conserve que nom de commande,
collection et indicateurs de portée owner/id/taille de liste. Aucune valeur UUID,
requête complète, document, nom privé ou secret n’est conservé. L’observateur
optionnel peut ainsi vérifier l’absence de page privée non bornée par owner, de
mutation sans owner et de lecture globale des films. Les tests sans serveur ne
valent pas exécution native : la preuve reste le reçu du snapshot réellement testé.

Le 12 novembre 2026, le snapshot de 57 fichiers (manifeste SHA-256
`981eaa9dd15b400ddc625933f38636a85ebd83cab5ee525ae0b1a8f8fde89ce3`)
a passé ce contrat sur MongoDB 8.0.34, avec identités de maintenance et applicative
distinctes. Les 310 observations de portée privée, la rétention exacte après
2501 événements, les annulations sur parents absents, le conflit concurrent et
les refus natifs de rang invalide ont été vérifiés. Le précontrôle a exécuté
51 lectures sans modifier session ni révision. Les contrats auth, films,
exemplaires et catalogue public ont également été réexécutés. Les deux conteneurs
et deux volumes jetables ont été supprimés ; le service stable est resté healthy.
Cette preuve concerne les services et dépôts natifs, pas une API HTTP entièrement
composée avec MongoDB. Le reçu privé conservé porte le SHA-256
`1a47d7e4c6138758b9017060817a7cae53f651160de9322bea55033a6a2f71f2`.
