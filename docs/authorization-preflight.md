# Autorisation préalable sans activité de session

La préparation HTTP des films, exemplaires et collections doit vérifier
l’autorité avant parsing, préconditions et éventuel appel de métadonnées. Elle
utilise désormais `authenticated_query`, qui partage **exactement** le même
chargement session/compte et `session_is_current` que la commande `authenticated`.
Elle s’exécute avec `Persistence.query` : aucune actualisation de last_seen ou
révision applicative, et les repositories sont en lecture seule.

La commande finale continue d’utiliser `Persistence.run`, relit compte/session,
vérifie rôle/propriété/version et touche la session dans la même transaction que
la mutation. Une révocation pendant un appel externe est donc toujours refusée.
Le changement ne déplace aucune autorisation vers le frontend et ne fait entrer
ni Flask ni requête réseau dans un callback de persistance.

## Correction et changement de comportement explicites

L’ancien précontrôle employait `authenticated` et committait une activité de
session avant lecture du corps. Un POST malformé400 ou invalide422 pouvait donc
incrémenter la révision et prolonger la session sans mutation métier ; un succès
utilisait deux commandes committées. Le défaut a été reproduit sur les trois
domaines à horloge1001 avec une session créée à1000.

Les précontrôles sont maintenant sans effet applicatif, succès ou refus. Les
erreurs400/422 et les préconditions428/412 avant parsing ne modifient plus les
octets JSON, la révision ou last_seen. Un succès utilise un précontrôle en lecture
et une seule commande de mutation. Les validations qui lèvent422 dans la commande
finale annulent celle-ci, y compris son touch de session.

Cette nuance est **intentionnelle** : des refus métier retournés auparavant par un
précontrôle pouvaient prolonger la session. Il ne s’agit pas d’une parité cachée.
Les refus métier normaux de la commande finale et les GET privés qui emploient
`authenticated` gardent leur politique de transaction/touch existante ; ce correctif
ne prétend pas rendre tous les refus HTTP de toute l’application sans écriture.

## Preuves ciblées

`tests/test_readonly_authorization.py` contrôle les trois domaines : corps malformé,
attributs invalides, précondition absente/périmée avant parsing, octets/révision/
session inchangés, puis succès avec une seule commande et last_seen actualisé.
Un test révoque les sessions pendant le chargement externe de métadonnées et
vérifie401 sans film créé. Le helper `exercise_readonly_authorization(database,
observer=None)` exécute aussi les précontrôles des trois services sur le vrai
moteur, vérifie révision/session, refuse un touch dans une query, et conserve un
traceur minimal de lectures sans DML avant de tester la commande finale.

La CI Mongo permanente appelle ce helper avec un vrai CommandListener. Les
résultats natifs doivent rester liés au snapshot exécuté : un test local sans
serveur ne remplace pas cette preuve. Les anciennes validations C2a/C2b restent
historiques ; le nouveau précontrôle requiert les nouvelles preuves de ce lot.
