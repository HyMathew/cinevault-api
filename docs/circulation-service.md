# Prêts et réservations — service commun C4

`CirculationService` porte les prêts, retours, annulations, réservations, FIFO et
disponibilité. Les routes transmettent des valeurs Python, puis rendent un
`ServiceResult` après commit. Aucun accès Flask, PyMongo, réseau externe, horloge
ou générateur aléatoire ne se trouve dans le callback métier réexécutable.

Ce lot extrait la circulation ; il **n’active pas encore le backend Mongo pour
toute l’API**. La composition HTTP courante conserve JSON tant que les autres
routes documentaires ne sont pas extraites. Aucun fallback ni double écriture.
Le backend JSON réel reste requis par RT0705.

## Autorité et contrôles

Compte et session sont relus dans la transaction finale par `authenticated`.
Un membre ne consulte ni n’annule les prêts/réservations d’un autre membre ; ID
tiers et absent donnent404. Manager/admin peuvent gérer ces ressources. Les
listes filtrent scope, propriétaire, statut et film avant count/pagination ; les
profils publics et films joints concernent les seuls IDs de la page, au plus100.
Les pages utilisent24 éléments par défaut, au plus100, offset0..10000.

Les transitions existantes restent : pending→active→returned et
pending→cancelled ; waiting→fulfilled/cancelled/expired. Pas de suppression
d’historique. Les transitions demandent If-Match : version absente428,
périmée412 ; état incompatible409. Un retour ne crée et n’attribue aucun prêt.
Une réservation annulée ne supprime pas une demande de prêt distincte.

L’approbation exige manager/admin, vérifie l’état du bénéficiaire, ses cinq prêts
actifs maximum, la copie du même film, non abîmée et sans prêt actif, puis FIFO.
Les demandes pending et attentes waiting sont chacune limitées à dix par membre.
`due_at` est un entier exact (bool refusé), futur et au plus90jours après
approbation. Le retard est dérivé par `due_at < timestamp`, sans nouveau statut.

L’ordre des contrôles est conservé : autorité avant payload ; à approve,
rôle/maintenance/identité/ETag/état/bénéficiaire avant décodage ; return vérifie
le corps vide avant le rôle, après authentification. Les annulations vérifient
le corps vide avant la propriété/version. La frontière capture le corps borné,
y compris un rejet413, et diffère son exception jusqu’au point de contrôle
historique. Ainsi401/403/428 ne sont pas remplacés prématurément par413.

## Disponibilité et priorité

`total` inclut les exemplaires abîmés ; `loaned` compte les prêts actifs ;
`free` exclut exemplaires abîmés et occupés ; `reserved` compte les attentes
waiting dont expires_at est strictement futur. `available=max(0,free-reserved)`.
La réponse publique ne contient aucune identité d’emprunteur.

Un membre en file doit être la tête, même en présence de copies excédentaires.
Un membre hors file ne peut consommer qu’un surplus : `free > reserved`.
La tête est remplie dans la même transaction que l’approbation, liée au prêt,
avec audit reservation.fulfilled avant loan.approved. Ce lien reste après retour.
La création assigne `sequence=work.revision`, déjà la prochaine révision dans les
deux moteurs ; elle n’ajoute pas1 une seconde fois. Séquence unique conservée sur
tout l’historique, indépendante de la précision de l’horloge.

Les positions sont calculées dans toute la file sans exposer ses autres membres.
Seule la tête visible ayant une copie libre apparaît dans ready_ids. Les liens
open_loan_ids concernent les couples membre/film de la page, même pour une entrée
historique lorsqu’un nouveau prêt est ouvert.

## Expirations et échecs

Les endpoints réservations ainsi que approve/options clôturent globalement les
waiting échues. Le GET de disponibilité ignore les attentes échues sans les
modifier ; les GET prêts et les retours ne déclenchent pas cette maintenance.

Maintenance, mutation, session/révision, audit, registre UUID et éventuel reçu
partagent une seule UoW. **Un refus métier retourné normalement peut committer
les expirations**, par exemple un404 ou412 après maintenance. Une exception de
corps400/415/422/413 annule toute la transaction, y compris ces expirations et le
touch de session. Ne pas convertir ces exceptions en résultats committables.
À la seconde d’échéance, une annulation avec l’ancien ETag waiting reçoit412
après expiration ; avec l’ETag expired courant, elle reçoit409 d’état.

### Clarification v2 de l’ordre

Les **nouvelles expirations groupées suivent sequence croissante dans les deux
moteurs**. Le legacy parcourait le tableau JSON, qui pouvait avoir été réordonné
dans un snapshot pourtant valide. C’est une évolution documentée de comportement
de v2, pour un ordre déterministe cohérent avec FIFO ; pas une parité historique
prétendue. Les événements antérieurs gardent leur ordre sous la rétention déclarée
des2000derniers appends ; aucune réécriture ni nouvel ordinal de réservation.
L’ordre des annulations administratives JSON reste celui du tableau existant.

Les UUID et le timestamp sont préparés une fois hors callback. Pour un nombre
inconnu d’expirations, UUIDv5 dérive un ID d’événement depuis un namespace UUIDv4
préparé hors callback, l’action autorisée et l’UUID canonique de réservation.
Cette dérivation SHA-1 standard n’est utilisée que pour IDs non secrets, jamais
pour sessions, mots de passe ou preuve d’autorité. Une collision de registre
annule ; aucun nouveau nonce n’est inventé dans le callback pour la contourner.

Les batches d’expiration sont au plus500, consommés dans une seule UoW, sans
commit intermédiaire. Ce batch ne prétend pas borner toute la mémoire ou la durée
d’une transaction globale : un magasin très accumulé peut encore aboutir à503.

## Reçus facultatifs et résultat incertain

POST loans et reservations acceptent Idempotency-Key optionnelle,16..128 ASCII
`[A-Za-z0-9_-]`, opérations `loan.request` et `reservation.create`. Le namespace
acteur-clé est commun à copies/import ; mêmes clé et contenu valides rejouent201
avec le même objet initial, ETag, Location et Idempotency-Replayed:true ; autre
contenu/opération409. Autre acteur : clé indépendante. Les appels sans clé
gardent leur comportement métier mais ne bénéficient pas d’un reçu.

Le reçu, créé avec l’entité, conserve seulement la réponse publique initiale et
l’empreinte canonique de demande, pas le bearer/cookie/clé brute. TTL24h et budget
existant10000 reçus ; seuls les reçus expirés sont purgés. Après changement d’état,
le rejeu rend encore pending/waiting initial : suivre Location pour l’état courant.
Le validateur ne confond pas cette réponse historique avec une entité vivante.

TransactionUnavailable et CommitOutcomeUnknown sont rendus503 sans message du
driver/URI ; commit_unknown indique que le commit peut avoir réussi. Une création
à clé se réessaie **manuellement avec la même clé et le même corps** ; aucun retry
automatique du formulaire. Les transitions n’ont pas de reçu dans C4 : réconcilier
par GET avant une nouvelle action ; un412 ultérieur ne prouve pas que le premier
commit a échoué. Ne pas rafraîchir automatiquement l’ETag pour forcer une mutation.
Le raccord des clés aux formulaires Flask/Jinja est un lot Web distinct.

## Persistance et validation native

Le coordinateur global existant est la première écriture Mongo ; il sérialise les
commandes pour protéger quotas/FIFO. Snapshot/majority et retries bornés restent
ceux du runner. Cela limite le débit ; ce n’est pas une preuve de scalabilité ou
de haute disponibilité. Un commit qui reste inconnu réessaie le même commit, pas le callback.
Le protocole distinct du driver reste conservé : un commit ensuite refusé avec
`TransientTransactionError` permet une nouvelle transaction ; Unknown répété,
deadline ou erreur non labellisée après Unknown ne permettent aucun rejeu du corps.

La politique opérationnelle du runner conserve un budget de10s par défaut et
une limite séparée de5tentatives du même commit (pauses au plus20ms). Pour les
conflits transitoires du corps, le défaut devient64tentatives, avec backoff
exponentiel et jitter uniforme complet : fenêtre initiale20ms, puis40/80/160/320ms,
plafonnée à500ms ; chaque attente est tronquée au temps restant. Les paramètres
explicites `timeout_seconds`, `max_attempts` et `max_commit_attempts` conservent
leur sens : demander3tentatives borne toujours à3corps. Les erreurs non labellisées
ne sont pas rejouées. Aucun retry extérieur au runner n'est ajouté.

Cette évolution répond à un conflit natif112/TransientTransactionError épuisé
après85ms alors que le gagnant termine en99ms ; les deux pauses fixes préexistantes
pouvaient abandonner avant la libération du coordinateur malgré le budget restant.
Le jitter désynchronise les concurrents ; le plafond500ms limite chaque attente.
Sur63pauses, leur somme moyenne théorique à fenêtres pleines vaut14,81s : le
budget10s peut ainsi servir de borne temporelle plutôt que les deux anciennes
pauses. Le cap64 borne aussi le cas de jitter nul. Ces valeurs sont une politique
bornée, pas une garantie de progrès, d'équité ou de débit. Le délai se vérifie avant
chaque nouveau corps et commit ; CSOT borne les appels driver mais ne préempte pas
le travail CPU Python du callback. La validation déterministe simule une libération
à20/120/500/2000ms et une contention persistante ; elle ne remplace pas les courses
Mongo réelles sous la contrainte CPU cible. Une indisponibilité reste possible si
la contention épuise ce budget ; aucun503 n'est accepté à la place des résultats
métier attendus dans les tests de courses courtes.

Les repositories ciblent film, propriétaire, identité ou page ; inventory utilise
une anti-jointure sur les seuls exemplaires du film. Les validations finales
vérifient parents des prêts/réservations, lien fulfilled et quotas par compte,
avec des lectures ciblées, sans snapshot BSON global. Les index uniques des prêts
actifs/couples ouverts, attentes, séquences et prêts remplis restent des garde-fous.
Trois index de tri sans statut s’ajoutent : loan_time_order, borrower_time_order,
user_reservation_order ;28index déclarés au total, plus _id automatiques.
Une ancienne base doit migrer explicitement ses validateurs de reçus ; provision
refuse la divergence sans collMod implicite.

Le format JSON accepte lui aussi deux nouvelles opérations de reçus. Un ancien
binaire antérieur à C4 ne sait pas relire ces reçus et peut refuser le magasin :
un rollback après écritures C4 exige une sauvegarde/restauration ou conversion
contrôlée avec rapprochement des écritures, pas un simple retour de l’image.
Ne supprimer ni prêts ni réservations pour rendre cet ancien binaire compatible.

Les caps physiques JSON globaux10000 prêts/réservations et les constats de
capacité/admission équitable restent **ouverts**. Aucun historique n’est purgé
pour gagner de la place ; aucune copie de ce cap physique n’est imposée à Mongo.

## Reproduire les contrôles

Depuis un clone de l’API, Python3.13 et les dépendances déclarées installées :

```text
python -m pip install -r requirements-dev.txt
python -m pytest tests/test_circulation_model.py tests/test_circulation_service.py tests/test_circulation_http.py tests/test_circulation_integrity.py tests/test_circulation_constraints.py tests/test_circulation_concurrency.py tests/test_circulation_atomicity.py -q -W error
python -m pytest -q -W error
```

Les tests JSON utilisent de vrais fichiers temporaires et deux processus par
course : dernier exemplaire, reçu identique, attente dupliquée, annulation/FIFO,
tête contre suivant, retour contre attribution et double annulation. Ils
vérifient aussi501expirations en batches500+1 dans une seule transaction, un
snapshot réordonné, rollback, FK, quotas et stabilité des IDs après abort/retry.
Neuf injections après de vraies écritures vérifient l’annulation entrée/lien/
audit/reçu ; le contrôle natif vérifie aussi l’absence d’UUID orphelin.

Sur un hôte Linux non root avec Docker et noyau pris en charge, le gate public
[check_mongodb.py](../scripts/check_mongodb.py) provisionne un replica set jetable
authentifié et des comptes séparés, exécute ces contrats et nettoie ses ressources :

```text
python scripts/check_mongodb.py
```

Le helper natif reçoit explicitement la configuration des processus ; sans elle,
native_process_races.checked=false, jamais une prétention de courses exécutées.
Le gate la transmet en mémoire et ne la conserve pas dans ses reçus. Le traceur
ne garde que commandes, collections et indicateurs de portée/taille, aucun UUID,
document, token ni mot de passe.

Le helper collecte executionStats pour les trois index ajoutés, d’abord selon
le choix de l’optimiseur, puis avec hint explicite pour prouver leur capacité à
servir le tri sans SORT bloquant. Les nombres concernent la fixture synthétique
du gate, pas un benchmark de charge ni une promesse de choix automatique sur un
autre volume ; aucun filtre ou identifiant de membre n’est conservé dans le reçu.

Le test de commit incertain effectue un vrai commit Mongo puis masque sa réponse
par injection de test : une exécution du corps, deux tentatives du même commit,
puis reçu retrouvé sans doublon. Ce n’est pas une panne réseau réelle. Les résultats
natifs doivent être rattachés au snapshot effectivement exécuté ; les tests locaux
sans serveur et les outils privés de mission ne remplacent pas ce gate public.
