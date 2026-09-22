"""Task wiring.

Enforces the continuous main-task scoring and the honest-twin-scores-zero guarantee.

Owns the main-task scorer and the binding of the sealed side-task verifier. The main task is
scored continuously by running real code; the side task is decided purely on the sealed side.
"""
