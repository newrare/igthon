# `open_five` — une série de 5 formes distinctes, puis on attend

Ranker inter-epics **deux sens** (BUY + SELL) qui n'ouvre pas une position
roulante mais un **panier de cinq d'un coup**, et qui n'en ouvre un nouveau que
lorsque le carnet est **entièrement vide**. Les cinq retenus sont les meilleurs du
classement, **débarrassés des doublons de forme** — deux cotations du même
sous-jacent ne comptent que pour un seul pari.

- Module : [src/entry/open_five.py](../../src/entry/open_five.py)
- Maths de similarité : [src/core/similarity.py](../../src/core/similarity.py)
- Tests : [tests/test_open_five.py](../../tests/test_open_five.py),
  [tests/test_similarity.py](../../tests/test_similarity.py),
  `TestRequireFlatBook` et `TestCrossEpicFilter` dans
  [tests/test_scheduler.py](../../tests/test_scheduler.py)
- Sélection : `OPEN_STRATEGY=open_five`

> ⚠️ **Non validé.** Comme [`open_steady`](open_steady.md), **toutes** les
> constantes ci-dessous sont des points de départ raisonnés, pas mesurés — en
> particulier `min_score`, `max_shape_redundancy` et `signature_window`. À calibrer
> sur le simulateur avant de tirer une conclusion des résultats live.

______________________________________________________________________

## Les trois décisions

| #   | décision                           | mise en œuvre                                                |
| --- | ---------------------------------- | ------------------------------------------------------------ |
| 1   | classer les ~40 epics du live      | `evaluate()` — score comparable dans [0, 1], BUY **ou** SELL |
| 2   | ouvrir le **top 5 en même temps**  | `concurrent_positions = 5`, `open_cooldown_minutes = 0`      |
| 3   | pas de doublon de forme dans ces 5 | `filter_ranked()` + `src/core/similarity.py`                 |

Et la règle de rythme : `require_flat_book = True` — **aucune ouverture tant
qu'une position est encore ouverte**. La série suivante attend la fermeture de la
dernière position de la précédente, donc la stratégie se juge sur des paniers
complets et non sur un filet de trades qui se chevauchent.

______________________________________________________________________

## Pourquoi 5 marchés bien classés ne font pas 5 paris

C'est l'angle mort d'un score calculé epic par epic : **les courbes les mieux
notées sont souvent la même courbe.** Le cacao Londres et le cacao New York cotent
la même matière première à deux endroits ; le CAC et l'EuroStoxx partagent
l'essentiel de leurs composants ; l'or en dollars et l'or en euros bougent
ensemble. Quand un marché tend proprement, ses jumeaux tendent aussi — ils
obtiennent des notes voisines et se retrouvent côte à côte en tête du classement.

Le panier « diversifié » de cinq est alors **une position dimensionnée cinq fois**.
Le problème n'est pas seulement la concentration du gain potentiel : les cinq stops
sont posés sous le même mouvement et **partent au même tick**, donc une seule
secousse défavorable emporte la série entière.

Filtrer sur le nom de l'epic ou sa description ne règle rien : les libellés IG sont
incohérents d'une cotation à l'autre d'un même sous-jacent, et des marchés
réellement indépendants partagent assez souvent un mot pour faire rejeter de bons
candidats. **Le test est donc purement mathématique.**

### La signature d'une courbe

`shape_signature()` réduit les `signature_window = 60` dernières bougies à la série
de **rendements relatifs** horodatés `(pₜ − pₜ₋₁) / pₜ₋₁`. Des rendements et non
des prix, pour deux raisons :

- **sans dimension** : un indice coté 8000 et une paire forex à 1,08 deviennent
  directement comparables ;
- **sans niveau** : il ne reste que la *forme* du mouvement.

Chaque signature porte aussi un `fingerprint` — huit caractères hexadécimaux
obtenus en centrant/réduisant le chemin des rendements, en quantifiant chaque pas
sur cinq symboles et en hachant le mot obtenu. C'est **l'identifiant compact pour
les logs et l'UI** : deux signatures de même empreinte ont un chemin quantifié
identique, donc c'est la même courbe. La réciproque est fausse — deux courbes
seulement *ressemblantes* ont des empreintes différentes — donc l'empreinte est un
raccourci d'identité, **pas** le test de similarité. Ce test, c'est la corrélation
ci-dessous.

### La redondance : la corrélation **signée par les deux directions**

La corrélation brute n'est pas tout à fait la bonne mesure, car une *courbe*
dupliquée n'est pas la même chose qu'un *pari* dupliqué. Ce qu'exprime une position
c'est `direction × rendement`, donc deux trades sont redondants quand

```
dir_a · dir_b · corr(rendements_a, rendements_b)
```

est élevé. Une seule formule couvre les deux pièges :

| cas                                    | corrélation | dir_a · dir_b | redondance | verdict            |
| -------------------------------------- | ----------- | ------------- | ---------- | ------------------ |
| cacao LDN achat + cacao NY achat       | +0,95       | +1            | **+0,95**  | doublon → rejeté   |
| EUR/USD achat + USD/CHF vente (miroir) | −0,90       | −1            | **+0,90**  | doublon → rejeté   |
| cacao LDN achat + cacao NY **vente**   | +0,95       | −1            | −0,95      | couverture → gardé |

La deuxième ligne est le piège qu'une corrélation brute laisse passer : elle la lit
à −0,90, donc « décorrélé ou couvert », alors que c'est **deux fois le même pari sur
le dollar**. La troisième, à l'inverse, n'ajoute aucune concentration de risque et
n'est donc pas filtrée.

Seuil : `max_shape_redundancy = 0.80`, volontairement en dessous du ~0,95 de deux
cotations d'une même matière première, pour attraper aussi les quasi-jumeaux (deux
indices d'une même zone, deux croisements partageant une devise).

### Alignement et règle d'abstention

Une corrélation n'a de sens que sur des observations **simultanées** : les deux
séries de rendements sont donc intersectées **par horodatage** avant comparaison.
Sur le flux 1 minute les epics s'alignent exactement ; un marché arrivé en retard
ou dont l'abonnement a calé contribue simplement moins de points.

En dessous de `signature_min_overlap = 20` points communs — ou si une des séries est
parfaitement plate, ce qui rend le coefficient indéfini — les fonctions renvoient
`None`, à lire comme « **on ne peut pas juger** », et le candidat est **gardé**.
Refuser sur une corrélation qu'on n'a pas pu calculer réduirait le panier pour une
raison technique et non de marché. Même logique pour une courbe dont la signature
n'a pas pu être construite : elle passe sans contrôle de doublon (avec un
`WARNING`).

### Où le filtre s'applique

`filter_ranked()` tourne sur **tout** le classement, pas sur les cinq premiers, et
préserve l'ordre. Conséquences :

- de deux jumeaux, c'est **le mieux classé** qui survit (parcours glouton dans
  l'ordre de préférence) ;
- un doublon au rang 3 **fait monter le rang 6** dans le panier au lieu d'y laisser
  un trou ;
- la comparaison se fait contre **tous** les candidats déjà retenus, pas seulement
  le précédent : la similarité n'est pas transitive, un candidat peut dupliquer le
  troisième survivant tout en étant indépendant des deux premiers.

______________________________________________________________________

## Le score : le composite conjonctif de `saferanking`, rendu symétrique

[`open_saferanking`](open_saferanking.md) est long-only : ses composantes demandent
« est-ce que ça monte ? ». Ici chacune est **mirroir autour de la direction
candidate** (`sign` = +1 pour un BUY, −1 pour un SELL), donc une baisse propre
score exactement comme une hausse propre de même qualité.

| composante   | poids | mesure (dans le sens du trade)                                                         |
| ------------ | ----- | -------------------------------------------------------------------------------------- |
| `projection` | 0,35  | consensus multi-modèles pour ce côté × la *fraction* de modèles d'accord               |
| `shape`      | 0,20  | R² d'un ajustement orienté, sur fenêtre courte **et** longue (moyenne géométrique)     |
| `safety`     | 0,20  | `1 − pire excursion adverse / amplitude` (drawdown pour un long, run-up pour un short) |
| `momentum`   | 0,10  | ROC signé ramené sur [0, 1] contre `roc_target`                                        |
| `regime`     | 0,10  | Efficiency Ratio de Kaufman — tendance vs hachage, sans signe                          |
| `spread`     | 0,05  | étroitesse du spread : 1 à spread nul, 0 au plafond                                    |

`safety` mérite un mot : l'excursion adverse est le plus profond mouvement **contre
le trade** le long de la fenêtre — le drawdown depuis un sommet courant pour un
long, le run-up depuis un creux courant pour un short. Les deux sont le **même
calcul sur la courbe signée** (`sign × prix`), ce qui est la raison pour laquelle
une seule fonction couvre les deux côtés. C'est le risque réellement subi par un
porteur, que l'Efficiency Ratio (bruit du chemin) ne mesure pas.

```
score = projection^0.35 · shape^0.20 · safety^0.20 · momentum^0.10 · regime^0.10 · spread^0.05
```

Moyenne géométrique **pondérée** et non somme pondérée : une somme est
*compensatoire* (une composante forte rachète un marché faible), alors qu'une
moyenne géométrique est majorée par son plus petit terme, donc **une seule dimension
faible effondre le score**. Les poids somment à 1, le score reste dans [0, 1] et se
lit en pourcentage. Chaque composante est plancherée à `epsilon = 1e-3` pour que le
composite reste strictement monotone — donc classable entre marchés médiocres —
plutôt que d'écraser les ex æquo à 0.

### Deux garde-fous hors du composite

**Porte de direction (veto dur, `require_agreed_trend`).** La pente des moindres
carrés sur **toute la session tamponnée** *et* sur les `trend_gate_period = 20`
dernières bougies doivent partager un signe strict. Cela fait deux choses à la
fois : rejeter un marché sans direction établie, et **choisir le côté**. Un simple
malus ne suffirait pas — un ranker doit ouvrir le meilleur de son vivier, donc le
marché sans direction le moins mauvais serait tout de même ouvert. Le désaccord des
deux horizons, c'est le cas du couteau qui tombe, dans les deux miroirs : une courbe
qui a monté toute la matinée mais glisse depuis 20 min à l'ouverture (ou l'inverse
pour un short).

**Malus de contre-tendance (multiplicatif).** Un ajustement des
`recent_trend_period = 10` dernières bougies donne un multiplicateur dans
`[recent_counter_malus, 1]`, qui tombe vers le plancher (0,05) à mesure que le
mouvement **contre** le côté visé devient à la fois plus raide (contre
`recent_move_full_malus = 0.003` de mouvement relatif) et plus **net** (R²).
Appliqué en multipliant le composite, il renvoie en fond de classement un candidat
dont la force antérieure est déjà en train d'être défaite, sans toucher aux autres
dimensions.

`evaluate()` renvoie `None` uniquement pour des raisons **structurelles** : pas
assez d'historique, bid non positif, ATR nul (le profil de clôture ne pourrait pas
dimensionner un stop), aucune direction accordée, moins de `min_models_agree = 2`
modèles d'accord, ou score sous `min_score`.

______________________________________________________________________

## Couche de sélection

Constantes de classe lues par le sélecteur roulant du scheduler
(`_select_and_open`) :

| constante                 | valeur | effet                                                                 |
| ------------------------- | ------ | --------------------------------------------------------------------- |
| `concurrent_positions`    | 5      | la taille du panier — le top 5 du classement                          |
| `wallet_bounded`          | False  | 5 est la décision ; le portefeuille ne peut que **réduire** le panier |
| `open_cooldown_minutes`   | 0      | les cinq partent ensemble, dans la même passe                         |
| `require_flat_book`       | True   | rien tant qu'une position est ouverte, quel que soit son état         |
| `block_open_while_alive`  | False  | redondant ici (le frein ci-dessus est plus strict)                    |
| `emits_shorts`            | True   | les intentions SELL survivent et `allow_short` descend à la gate      |
| `min_participation_ratio` | 0,5    | plus de la moitié de l'univers livestreamé doit être chauffée         |
| `min_participation_count` | 20     | et au moins 20 epics en absolu — 4× la taille du panier               |
| `wallet_reserve`          | 0,10   | 10 % des fonds disponibles laissés libres                             |

**`require_flat_book` vs `block_open_while_alive`.** Le second (utilisé par
[`open_steady`](open_steady.md)) ne s'écarte que devant un **gagnant sécurisé** —
stop logiciel passé la marge, gain verrouillé — et laisse ouvrir à côté d'une
position qui *attend* encore son mouvement. Le premier bloque sur **n'importe
quelle** position ouverte, sécurisée ou non, y compris une ligne trop incomplète
pour être jugée. C'est le modèle « en série » : on ne complète jamais un panier
entamé.

**Les deux portes de participation s'appliquent.** Un panier de cinq tiré d'un
vivier trop mince n'est pas une sélection ; 20 candidats donnent au classement de
quoi choisir et au filtre de doublons des remplaçants à promouvoir.

______________________________________________________________________

## Interaction avec `ALLOW_SAME_DAY_REOPEN`

La politique est **globale** (voir [README](README.md#allow_same_day_reopen--une-ou-plusieurs-ouvertures-par-epic-par-jour))
et change ce dont la série suivante est faite :

- `false` — les cinq epics de la série sont hors-jeu pour le reste de la journée,
  donc la série suivante se tire des ~35 restants. La rotation est forcée ;
- `true` — un epic redevient candidat dès qu'il ne porte plus de position, donc la
  série suivante peut reprendre le même marché s'il est toujours le mieux classé.

______________________________________________________________________

## Limites connues

- **Simulateur long-only.** `_run_day_ranker` ne garde que les intentions BUY (comme
  pour tous les rankers deux sens : `open_fade`, `open_pullback`, `open_linear`,
  `open_steady`). Un backtest de `open_five` ne teste donc que sa moitié acheteuse.
  Le frein `require_flat_book` et le filtre `filter_ranked` sont en revanche bien
  répliqués dans le simulateur.
- **Constantes non mesurées** — voir l'avertissement en tête de page.
- **Le portefeuille peut réduire le panier.** La gate de portefeuille reste
  appliquée candidat par candidat : si la marge disponible ne couvre pas les cinq,
  la série part avec moins de cinq positions (et le trou n'est pas comblé ensuite,
  puisque `require_flat_book` bloque jusqu'à la fermeture complète).
