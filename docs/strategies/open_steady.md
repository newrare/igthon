# `open_steady` — la courbe la plus régulière des 10 dernières minutes

Ranker inter-epics **deux sens** (BUY + SELL). Il note la **régularité** de la
courbe récente ; le signe de la pente ne choisit que le côté. Une hausse propre
s'achète, une baisse propre se vend.

- Module : [src/entry/open_steady.py](../../src/entry/open_steady.py)
- Tests : [tests/test_open_steady.py](../../tests/test_open_steady.py),
  `TestMinParticipationCount` et `TestBlockOpenWhileAlive` dans
  [tests/test_scheduler.py](../../tests/test_scheduler.py)
- Sélection : `OPEN_STRATEGY=open_steady`

> ⚠️ **Non validé.** Contrairement à [`open_fade`](open_fade.md) (dont les seuils
> viennent d'un replay sur ~100 000 résultats résolus), **toutes** les constantes
> ci-dessous sont des points de départ raisonnés, pas mesurés — en particulier
> `min_score`, `move_target` et `max_step_share`. À calibrer sur le simulateur
> avant de tirer une conclusion des résultats live.

______________________________________________________________________

## Le problème : aucun indicateur seul ne rejette les trois défauts

Le cahier des charges nomme trois formes à éviter et une à garder. Le tableau
montre pourquoi il faut quatre mesures :

| courbe                  | R²        | ER de Kaufman | plus grand pas | mouvement net |
| ----------------------- | --------- | ------------- | -------------- | ------------- |
| ligne régulière         | **élevé** | **élevé**     | **petit**      | visible       |
| zig-zag (monte/descend) | faible    | **faible**    | petit          | faible        |
| pic sur une bougie      | moyen     | **1.0**       | **dominant**   | visible       |
| droite mais plate       | **élevé** | **élevé**     | **petit**      | **~0**        |

La ligne « pic » est le piège. L'Efficiency Ratio vaut `|net| / Σ|pas|`, donc un
saut unique suivi du silence obtient **ER = 1.0 — son maximum** : le garde-fou
anti-hachage qui attrape le zig-zag **récompense** le pic. Le R² ne sauve pas non
plus (une fonction en marche s'ajuste médiocrement à une droite, pas
catastrophiquement). Détecter un pic exige donc sa propre mesure.

______________________________________________________________________

## Les quatre composantes

Calculées sur les `window = 10` derniers bid closes (~10 min sur le flux 1 min).

| #   | composante   | mesure                                   | défaut qu'elle rejette |
| --- | ------------ | ---------------------------------------- | ---------------------- |
| 1   | `linearity`  | R² de la régression                      | courbe qui plie        |
| 2   | `directness` | ER de Kaufman                            | zig-zag                |
| 3   | `smoothness` | parcours réparti également entre bougies | **pic**                |
| 4   | `visibility` | mouvement net / bid, saturation douce    | droite plate           |

**`smoothness` en détail.** `_step_concentration` renvoie
`max|pas| / Σ|pas|` : la part du parcours total portée par la plus grosse bougie.
Une droite régulière de `n` points étale son parcours sur `n − 1` pas égaux, donc
la part vaut son minimum structurel `1 / (n − 1)` (0,111 pour 10 points) ; un pic
la pousse vers 1. La composante mappe `1/(n−1)` → 1.0 et `max_step_share` → 0.0,
et une concentration **au-dessus** de `max_step_share` est un **veto dur**.

> **Contre-intuitif, et voulu :** `smoothness` mesure la *régularité* du parcours,
> pas sa *qualité*. Un zig-zag a tous ses pas égaux, donc il score **1.00** ici. Ce
> n'est pas un bug : le zig-zag est tué par `linearity` (~0,06) et `directness`
> (~0,12), le pic par `smoothness` seule. Chaque terme couvre un défaut que les
> autres ratent — c'est pourquoi il en faut quatre.

**`visibility` en détail.** `_saturate(m, move_target) = 1 − exp(−m / move_target)`
plutôt qu'un écrêtage `min(m / target, 1)`. Deux raisons :

- **strictement croissante**, donc deux courbes propres de vitesses différentes ne
  sont jamais ex æquo. Avec l'écrêtage dur, toute courbe au-delà de `move_target`
  marquait exactement `1.000`, et « ne garder que la meilleure » retombait sur
  l'ordre d'arrivée des epics dans `ranked.sort()` ;
- **rendements fortement décroissants** : à `move_target` → 0,63 ; ×2 → 0,86 ;
  ×3 → 0,95. Aller 4× plus vite ne rapporte que quelques points, ce qui est
  exactement la préférence demandée (propre plutôt que rapide), rendue
  structurelle et non pas indicative.

______________________________________________________________________

## Composition : moyenne géométrique, pas une somme

```
score = linearity^0.35 · directness^0.25 · smoothness^0.25 · visibility^0.15
```

Les poids somment à 1, donc le score reste dans [0, 1] et se lit en pourcentage.

Le choix est délibéré et reprend la leçon déjà inscrite dans
[`open_saferanking`](open_saferanking.md) : une somme pondérée est
**compensatoire**, donc une `visibility` élevée pourrait sauver un zig-zag et
couronner précisément la courbe que le cahier des charges demande d'éviter. Une
moyenne géométrique est majorée par son plus petit terme, donc **une seule
dimension faible effondre tout le score** — ce qui est le sens de « propre ET
nette ET pas un pic ET pas en zigzag ».

Chaque composante est plancherisée à `epsilon = 1e-3` pour que le composite reste
strictement monotone (encore classable entre marchés médiocres) au lieu de coller
les ex æquo à 0.

Les trois termes de régularité portent **0,85** du poids, `visibility` seulement
**0,15**.

### Ordres de grandeur mesurés sur courbes synthétiques

| courbe                | linearity | directness | smoothness | visibility | score     |
| --------------------- | --------- | ---------- | ---------- | ---------- | --------- |
| droite +0,5 pt/bougie | 1.00      | 1.00       | 1.00       | 0.24       | **0.811** |
| droite +2 pt/bougie   | 1.00      | 1.00       | 1.00       | 0.67       | **0.942** |
| droite +8 pt/bougie   | 1.00      | 1.00       | 1.00       | 0.99       | **0.998** |
| zig-zag               | 0.06      | 0.12       | 1.00       | 0.38       | **0.190** |
| droite plate          | 1.00      | 1.00       | 1.00       | ~0         | **0.355** |
| pic                   | —         | 1.00       | —          | —          | **veto**  |

`min_score = 0.60` sépare donc les tendances réelles (0,81+) du bruit (0,19) et
des courbes plates (0,36).

______________________________________________________________________

## Portes dures

| porte                            | rejet                                             |
| -------------------------------- | ------------------------------------------------- |
| `len(buf) < warmup` (30)         | moins de 30 relevés — pas candidat                |
| `bid <= 0`                       | structurel                                        |
| `_is_contiguous`                 | trou dans les 30 derniers relevés                 |
| `atr <= 0`                       | le close profile ne peut pas dimensionner un stop |
| `slope == 0`                     | aucun côté à prendre                              |
| `concentration > max_step_share` | **pic** (une bougie porte >50 % du parcours)      |
| `score < min_score`              | pas assez propre                                  |

**`_is_contiguous` — pourquoi.** Le buffer est un `deque` alimenté par le flux :
sa *longueur* dit combien de relevés sont arrivés, jamais s'ils sont consécutifs.
Une souscription figée laisse un trou qu'une régression lit comme un segment de
droite parfaitement légitime. La porte vérifie donc l'espacement des relevés
(`max_gap_seconds = 90` s sur un flux de 60 s : accepte la gigue normale, rejette
le décrochage). Aucune autre stratégie du dépôt ne fait cette vérification.

______________________________________________________________________

## Couche de sélection

| attribut                    | valeur | règle réalisée                                      |
| --------------------------- | ------ | --------------------------------------------------- |
| `emits_shorts`              | `True` | BUY et SELL ; lève la porte long-only               |
| `min_period` (via `warmup`) | `30`   | ≥ 30 relevés consécutifs pour être candidat         |
| `min_participation_count`   | `21`   | un classement valide exige **plus de 20** candidats |
| `min_participation_ratio`   | `0.0`  | désactivé : la règle est un compte, pas un ratio    |
| `block_open_while_alive`    | `True` | rien de neuf tant qu'un trade est **vivant**        |
| `wallet_bounded`            | `True` | on ouvre tant que le wallet couvre une marge        |
| `open_cooldown_minutes`     | `5`    | une ouverture par passe, espacées de 5 min          |
| `wallet_reserve`            | `0.10` | 10 % des fonds disponibles gardés libres            |

### `block_open_while_alive` — la définition de « vivant »

Implémenté par `BotScheduler._alive_positions`. Une position est **vivante** quand
les deux conditions tiennent :

1. son stop **logiciel** — `level_follower`, le niveau que le close profile fait
   respecter entre deux relevés de bid, **pas** `level_stop` qui est le stop
   courtier posé plus loin chez IG (un spread + coussin ATR au-delà du follower) —
   a cliqueté jusqu'à `level_margin` ou au-delà, donc le stop lui-même garantit un
   gain et ne protège plus seulement l'entrée ;
1. le prix de sortie live (bid pour un long, offer pour un short) est au-delà du
   break-even `level_zero`.

Les deux comparaisons sont écrites sur la distance signée, donc un short est jugé
par la même règle en miroir (`sign = −1`).

**Ce qui ne bloque pas, et c'est le point central :** une position qui *attend*
encore — plate depuis l'ouverture, oscillant entre le break-even et son stop, ou
en route vers le stop — n'est **pas** vivante et n'empêche aucune ouverture. Le
frein existe pour ne pas ajouter du risque à côté d'un gagnant *confirmé*, jamais
pour laisser un trade inerte s'asseoir sur une opportunité.

Deux comportements de repli, choisis dans ce sens :

- une ligne sans les niveaux nécessaires (position adoptée / héritée, ouverte sans
  `level_margin`) est rapportée **non vivante** — le frein ne gèle pas les
  ouvertures sur un inconnu ;
- la condition 1 est décisive (elle implique la 2 pour toute position dont le stop
  n'a pas encore sauté), donc un flux manquant ne « désécurise » pas un gagnant.

### Limite connue

La porte de participation du scheduler compte les epics avec
`len(buf) >= warmup` — soit **30 relevés**, sans vérifier leur contiguïté (la
contiguïté est testée dans `evaluate`, par epic). Un epic dont le flux a décroché
compte donc dans les « plus de 20 candidats » avant d'être écarté au scoring. Le
comptage est ainsi légèrement optimiste ; le corriger demanderait de déplacer le
test de contiguïté dans la couche de sélection.

______________________________________________________________________

## Comparaison avec les rankers voisins

| stratégie                         | fenêtre        | ce qui est noté                        |
| --------------------------------- | -------------- | -------------------------------------- |
| [`open_slope`](open_slope.md)     | ~10 min        | pente seule — le plus rapide gagne     |
| **`open_steady`**                 | **~10 min**    | **régularité (4 termes, conjonctifs)** |
| [`open_linear`](open_linear.md)   | session (≤200) | rectitude de la journée (2 termes)     |
| [`open_rebound`](open_rebound.md) | ~1 h           | forme en V                             |
| [`open_fade`](open_fade.md)       | ~1 h           | tendance étendue, prise à l'envers     |

`open_steady` est à `open_slope` ce que `open_saferanking` est à `open_ranking` :
même fenêtre, mais conjonctif au lieu de compensatoire, et la vitesse pure y est
délibérément dévaluée.
