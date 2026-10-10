"""Shared physical range-weight floor and soft-L1 loss scale."""

# Slant->horizontal legitimately produces very short radii (tracker nearly
# under a ceiling probe). For weighting, radii are clamped to this physical
# minimum, converted to each floor's pixel scale, so one reading cannot dominate.
MIN_WEIGHT_RADIUS_M = 0.5

# Robust-loss knee in the objective's dimensionless residual (relative range
# error times sqrt(reliability)). Share it with uncertainty estimation so a
# persistently conflicting reading loses influence consistently in both paths.
SOLVER_ROBUST_F_SCALE = 0.3
