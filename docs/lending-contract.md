# Contrat du jalon prêts et réservations

**État du jalon : prêts et réservations réalisés en local.** Les deux fonctions possèdent validateurs, routes et écrans. L'intégration réelle comptes/catalogue/collections/prêts/réservations a réussi 112 contrôles HTTP locaux. Les tests dédiés comprennent deux processus d'approbation du même exemplaire et deux processus de création d'une même attente, ainsi que l'expiration avec horloge contrôlée. Voir [les règles réalisées](reservations.md). Ces preuves ne valent ni contrôle navigateur complet, ni audit final, ni déploiement Swarm.

## Prêts

Un membre demande un film ; un gestionnaire ou administrateur attribue explicitement un exemplaire. Aucun client ne choisit le bénéficiaire d'une demande par `borrower_id`.

Objet prévu : id, movie_id, borrower_id, copy_id nullable, status, requested_at, approved_at nullable, due_at nullable, returned_at nullable, cancelled_at nullable. Dates en secondes UTC Unix.

- `pending` : demande sans exemplaire, dates d'approbation/retour/annulation nulles.
- `pending → active` : manager/admin, exemplaire du film, non abîmé, sans autre prêt actif, priorité des réservations respectée. approved_at ≥ requested_at ; échéance future, au plus 90 jours après l'approbation. Au plus cinq prêts actifs par compte.
- `active → returned` : manager/admin, returned_at ≥ approved_at ; conserve exemplaire et historique.
- `pending → cancelled` : propriétaire ou manager/admin ; aucune annulation d'un prêt actif, qui doit suivre un retour effectif.
- Toute autre transition : 409. Version absente/périmée : 428/412. Deux approbations simultanées du même exemplaire doivent produire exactement un succès.

Routes réalisées : POST/GET `/api/v1/loans`, GET `/api/v1/loans/{id}`, POST `/api/v1/loans/{id}/approve`, `/return`, `/cancel`. GET `/api/v1/loans/{id}/options` est réservé à la gestion et ne propose que les exemplaires libres non abîmés ; l'approbation revérifie cet état sous verrou. Les actions portent If-Match. Les membres ne voient que leurs prêts ; manager/admin voient les demandes nécessaires à leur gestion. Une demande ou un prêt actif identique pour le même membre/film est refusé ; dix demandes en attente maximum par membre. Pas de DELETE d'historique. L'approbation vérifie aussi l'état actif du compte bénéficiaire et sa limite de prêts.

Un prêt est en retard si actif et due_at < heure courante. Le retard est dérivé, sans mutation permanente d'un statut dépendant de l'horloge. Les listes peuvent renvoyer des IDs en retard séparément des entités versionnées.

## Réservations et ordre

Objet prévu : id, movie_id, user_id, status, sequence, created_at, expires_at, closed_at nullable, loan_id nullable.

Types de statut : waiting, fulfilled, cancelled, expired. Une demande crée sa propre réservation ; pas de user_id ni de priorité fournis par le client. Au plus une attente par membre/film et dix attentes par membre. L'ordre est le champ sequence unique, affecté sous verrou à partir de la prochaine révision globale ; il départage sans ambiguïté deux créations dans la même seconde. Expiration après quatorze jours.

Une réservation est admise lorsque tous les exemplaires prêtables sont empruntés ou déjà couverts par des attentes prioritaires. S'il existe un exemplaire libre non réservé, le membre doit demander un prêt. Une copie abîmée n'est pas prêtable.

À l'approbation d'un prêt, les réservations actives de ce film sont examinées dans l'ordre : un membre en file doit être en tête ; un membre hors file ne peut consommer qu'un exemplaire excédant le nombre d'attentes. L'approbation de la tête remplit sa réservation dans la même transaction et la lie au prêt. Le lien reste valide après le retour. Une attente expirée ne bloque jamais l'attribution.

Une attente peut être annulée par son propriétaire ou manager/admin. Les états fulfilled/cancelled/expired sont terminaux. closed_at renseigne l'instant de clôture. Seul fulfilled possède loan_id ; le prêt doit correspondre au même film et au même bénéficiaire.

Routes prévues : POST/GET `/api/v1/reservations`, GET `/api/v1/reservations/{id}`, POST `/api/v1/reservations/{id}/cancel`, version exigée pour l'annulation. Les membres voient leurs seules réservations ; les positions sont calculées sans exposer l'identité des autres membres. Pas de suppression d'historique.

## Disponibilité, références et audit

La disponibilité publique est calculée à partir des exemplaires non abîmés, des prêts actifs et des attentes non expirées. Elle n'expose aucun emprunteur. Au minimum : total d'exemplaires, prêts actifs, exemplaires prêtables libres et disponibles hors réservations. Un endpoint dédié évite de changer silencieusement la forme des entités persistées et leur ETag.

Toute référence historique de prêt ou réservation empêche la suppression du film ou de l'exemplaire concerné ; aucun effacement en cascade silencieux. La modification d'un exemplaire ne permet pas de changer movie_id.

Transitions et audit minimal sont écrits dans la même transaction. Étendre les types/actions d'audit aux prêts et réservations ; une expiration automatique doit identifier un acteur système (null) plutôt que l'attribuer artificiellement au lecteur. L'acteur null ne doit être permis que pour cette action définie. Conserver la rétention déclarée de 2000 événements et ne jamais ajouter un secret ou un corps de formulaire au journal.

## Vérifications à exécuter

1. Tests rouges des transitions, rôles, accès tiers, horodatages et références avant code.
2. Deux **processus** distincts tentent réellement d'approuver deux demandes du même exemplaire, via la même persistance : un actif seulement, pas de mutation partielle.
3. FIFO, annulation, expiration, attribution et retour ; disponibilité recalculée et historique cohérent.
4. JSON Schema, exemples et OpenAPI alignés sur les réponses et erreurs réelles.
5. Écrans membres/gestion, CSRF et versions, puis intégration HTTP Compose réelle et persistance après redémarrage.
6. Rejouer les suites existantes et documenter exactement l'environnement et les limites. Une conception ou un tableau initialisé ne valide aucune de ces fonctions.
