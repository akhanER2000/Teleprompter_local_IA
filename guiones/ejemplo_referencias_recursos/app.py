"""Ejemplo de código para el guion de referencias."""
from dataclasses import dataclass


@dataclass
class Venta:
    mes: str
    total: float


def crecimiento(ventas: list[Venta]) -> float:
    """Crecimiento porcentual entre el primer y el último mes."""
    if len(ventas) < 2 or ventas[0].total == 0:
        return 0.0
    return (ventas[-1].total / ventas[0].total - 1) * 100


if __name__ == "__main__":
    datos = [Venta("enero", 120.0), Venta("febrero", 135.5), Venta("marzo", 162.0)]
    print(f"Crecimiento del trimestre: {crecimiento(datos):.1f} %")
